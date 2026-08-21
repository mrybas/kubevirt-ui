package controller

import (
	"fmt"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/yaml"
)

// mustHostEndpoints puts the host apiserver's own record where the operator
// reads it. envtest runs an apiserver but no endpoints controller, so this is
// the fixture standing in for what a real cluster maintains itself.
func mustHostEndpoints(t *testing.T, addresses ...string) {
	t.Helper()
	slice := &discoveryv1.EndpointSlice{ObjectMeta: metav1.ObjectMeta{
		Namespace: "default", Name: "kubernetes",
		Labels: map[string]string{discoveryv1.LabelServiceName: "kubernetes"},
	}}
	slice.AddressType = discoveryv1.AddressTypeIPv4
	slice.Ports = []discoveryv1.EndpointPort{{Port: ptr.To[int32](6443)}}
	for _, address := range addresses {
		slice.Endpoints = append(slice.Endpoints, discoveryv1.Endpoint{
			Addresses:  []string{address},
			Conditions: discoveryv1.EndpointConditions{Ready: ptr.To(true)},
		})
	}
	existing := &discoveryv1.EndpointSlice{}
	err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "default", Name: "kubernetes"}, existing)
	switch {
	case apierrors.IsNotFound(err):
		if err := k8sClient.Create(testCtx, slice); err != nil {
			t.Fatalf("creating the host endpoints: %v", err)
		}
	case err != nil:
		t.Fatalf("reading the host endpoints: %v", err)
	default:
		existing.AddressType = slice.AddressType
		existing.Ports = slice.Ports
		existing.Endpoints = slice.Endpoints
		if err := k8sClient.Update(testCtx, existing); err != nil {
			t.Fatalf("updating the host endpoints: %v", err)
		}
	}
}

func mustCSICredential(t *testing.T, namespace, server string) {
	t.Helper()
	kubeconfig := fmt.Sprintf(`apiVersion: v1
kind: Config
clusters:
- name: infra-cluster
  cluster:
    server: %s
    certificate-authority-data: Zm9v
contexts:
- name: only-context
  context: {cluster: infra-cluster, user: kubevirt-csi, namespace: %s}
current-context: only-context
users:
- name: kubevirt-csi
  user: {token: t0ken}
`, server, namespace)
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: csiCredentialSecret}}
	secret.Data = map[string][]byte{"kubeconfig": []byte(kubeconfig)}
	if err := k8sClient.Create(testCtx, secret); err != nil &&
		!apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the credential: %v", err)
	}
}

// TestTheDriverReachesTheHostOverTheTransitPlane.
//
// Measured on the stand and the reason this exists: the credential named the
// host's management address, and the tenant VPC has exactly one route — the
// default, to the border. So every volume attach went out through the gateway,
// on the same path as the tenant's internet traffic, while the control plane
// beside it already rode the transit leg. A border outage left the VMs running
// and stopped every attach, detach and expand.
func TestTheDriverReachesTheHostOverTheTransitPlane(t *testing.T) {
	mustHostEndpoints(t, "10.198.160.4", "10.198.160.1", "10.198.160.2")
	mustNamespace(t, "tenant-thap", "")
	mustCSICredential(t, "tenant-thap", "https://10.198.175.250:6443")

	obj := vpcTalosTenant("thap")
	obj.Spec.Network = "net-thap"
	reconciler := transitReconciler("transit-hap")

	published, message, err := reconciler.ensureHostAPI(
		testCtx, obj, "tenant-thap", "10.199.0.210")
	if err != nil {
		t.Fatalf("ensureHostAPI: %v", err)
	}
	if !published {
		t.Fatalf("not published: %s", message)
	}

	service := &corev1.Service{}
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-thap", Name: "thap-host-api"}, service); err != nil {
		t.Fatalf("reading the service: %v", err)
	}
	if service.Spec.Type != corev1.ServiceTypeLoadBalancer {
		t.Errorf("type = %s", service.Spec.Type)
	}
	if service.Annotations["metallb.universe.tf/loadBalancerIPs"] != "10.199.0.210" {
		t.Errorf("it did not ask for the tenant's own address: %v", service.Annotations)
	}
	// Both services on one address must carry the key; annotating one leaves
	// the other pending for ever.
	if service.Annotations["metallb.universe.tf/allow-shared-ip"] != cpSharingKey("thap") {
		t.Errorf("no sharing key: %v", service.Annotations)
	}
	if len(service.Spec.Selector) != 0 {
		t.Errorf("a selector would match pods; the backends are not pods: %v",
			service.Spec.Selector)
	}
	if got := service.Spec.Ports[0]; got.Port != hostAPIPort || got.TargetPort.IntValue() != 6443 {
		t.Errorf("ports = %d -> %s, want %d -> 6443",
			got.Port, got.TargetPort.String(), hostAPIPort)
	}

	slice := &discoveryv1.EndpointSlice{}
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-thap", Name: "thap-host-api"}, slice); err != nil {
		t.Fatalf("reading the endpoints: %v", err)
	}
	if slice.Labels[discoveryv1.LabelServiceName] != "thap-host-api" {
		t.Fatalf("the Service will never find these: %v", slice.Labels)
	}
	var got []string
	for _, endpoint := range slice.Endpoints {
		got = append(got, endpoint.Addresses...)
	}
	// Sorted, so an unchanged set is an unchanged object rather than a patch
	// every pass.
	want := "10.198.160.1 10.198.160.2 10.198.160.4"
	if strings.Join(got, " ") != want {
		t.Errorf("endpoints = %v, want %s", got, want)
	}
}

// TestAControlPlaneNodeThatLeftStopsBeingABackend.
//
// The list is rebuilt rather than added to. An endpoint list that only grows
// sends a share of every request to an address that is not there, and the
// symptom is a driver that works most of the time.
func TestAControlPlaneNodeThatLeftStopsBeingABackend(t *testing.T) {
	mustHostEndpoints(t, "10.198.160.1", "10.198.160.2", "10.198.160.4")
	mustNamespace(t, "tenant-thgone", "")
	mustCSICredential(t, "tenant-thgone", "https://10.198.175.250:6443")

	obj := vpcTalosTenant("thgone")
	obj.Spec.Network = "net-thgone"
	reconciler := transitReconciler("transit-hgone")
	if _, _, err := reconciler.ensureHostAPI(
		testCtx, obj, "tenant-thgone", "10.199.0.211"); err != nil {
		t.Fatalf("first pass: %v", err)
	}

	mustHostEndpoints(t, "10.198.160.1", "10.198.160.2")
	if _, _, err := reconciler.ensureHostAPI(
		testCtx, obj, "tenant-thgone", "10.199.0.211"); err != nil {
		t.Fatalf("second pass: %v", err)
	}

	slice := &discoveryv1.EndpointSlice{}
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-thgone", Name: "thgone-host-api"}, slice); err != nil {
		t.Fatalf("reading the endpoints: %v", err)
	}
	for _, endpoint := range slice.Endpoints {
		for _, address := range endpoint.Addresses {
			if address == "10.198.160.4" {
				t.Fatalf("the node that left is still a backend: %v", slice.Endpoints)
			}
		}
	}
}

// TestATenantWithoutStorageIsNotHandedTheHostAPI.
//
// And a tenant that had it and stopped has the publication taken away. A port
// left open on a plane after the reason for it is gone is the same hole as an
// allow left behind by a departed tenant.
func TestATenantWithoutStorageIsNotHandedTheHostAPI(t *testing.T) {
	mustHostEndpoints(t, "10.198.160.1")
	mustNamespace(t, "tenant-thnos", "")
	mustCSICredential(t, "tenant-thnos", "https://10.198.175.250:6443")

	obj := vpcTalosTenant("thnos")
	obj.Spec.Network = "net-thnos"
	reconciler := transitReconciler("transit-hnos")
	if published, _, err := reconciler.ensureHostAPI(
		testCtx, obj, "tenant-thnos", "10.199.0.212"); err != nil || !published {
		t.Fatalf("published=%v err=%v", published, err)
	}

	credential := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: "tenant-thnos", Name: csiCredentialSecret}}
	if err := k8sClient.Delete(testCtx, credential); err != nil {
		t.Fatalf("removing the credential: %v", err)
	}

	published, _, err := reconciler.ensureHostAPI(
		testCtx, obj, "tenant-thnos", "10.199.0.212")
	if err != nil {
		t.Fatalf("ensureHostAPI: %v", err)
	}
	if published {
		t.Fatal("it is still published with nothing to use it")
	}
	service := &corev1.Service{}
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-thnos", Name: "thnos-host-api"}, service); !apierrors.IsNotFound(err) {
		t.Fatalf("the service is still there: %v", err)
	}
}

// TestTheGuardOpensTheHostAPIPortOnlyWhenItIsPublished.
func TestTheGuardOpensTheHostAPIPortOnlyWhenItIsPublished(t *testing.T) {
	mustHostEndpoints(t, "10.198.160.1")
	mustNamespace(t, "tenant-thgrd", "")
	mustTransitSubnet(t, "transit-hgrd")
	mustEIP(t, "cpt-eip-thgrd", "transit-hgrd", "10.199.1.70")

	obj := vpcTalosTenant("thgrd")
	obj.Spec.Network = "net-thgrd"
	reconciler := transitReconciler("transit-hgrd")

	rules := func() map[string]bool {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(ovnSubnetGVK)
		if err := k8sReader.Get(testCtx,
			types.NamespacedName{Name: "transit-hgrd"}, subnet); err != nil {
			t.Fatalf("reading the subnet: %v", err)
		}
		acls, _, _ := unstructured.NestedSlice(subnet.Object, "spec", "acls")
		out := map[string]bool{}
		for _, raw := range acls {
			rule, _ := raw.(map[string]any)
			out[fmt.Sprint(rule["match"])] = true
		}
		return out
	}
	port6444 := "ip4.src == 10.199.1.70 && ip4.dst == 10.199.0.213 && tcp.dst == 6444"

	if _, err := reconciler.ensureTransitACLs(testCtx, obj, "tenant-thgrd",
		"transit-hgrd", "10.199.0.0/22", "10.199.1.70", "10.199.0.213"); err != nil {
		t.Fatalf("without storage: %v", err)
	}
	if rules()[port6444] {
		t.Error("the port is open with nothing behind it")
	}

	mustCSICredential(t, "tenant-thgrd", "https://10.198.175.250:6443")
	if _, _, err := reconciler.ensureHostAPI(
		testCtx, obj, "tenant-thgrd", "10.199.0.213"); err != nil {
		t.Fatalf("publishing: %v", err)
	}
	if _, err := reconciler.ensureTransitACLs(testCtx, obj, "tenant-thgrd",
		"transit-hgrd", "10.199.0.0/22", "10.199.1.70", "10.199.0.213"); err != nil {
		t.Fatalf("with storage: %v", err)
	}
	if !rules()[port6444] {
		t.Error("published and unreachable — the guard is a whitelist")
	}
}

// TestTheCopyInsideTheTenantIsPointedAtTheTransitAddress.
//
// The host-side secret keeps its own address: it is the product's, and correct
// for a reader on the host network. Only the copy the driver opens is moved.
//
// `tls-server-name` is the part that makes this possible at all. The VIP is in
// no SAN of the host apiserver's certificate and cannot be — those are per node,
// and a per-tenant address cannot be added to them. Every one of them carries
// `kubernetes.default.svc`, so that is what the client verifies while connecting
// to the address. The alternative was one line of insecure-skip-tls-verify and
// a transit plane where anyone could be the host apiserver.
func TestTheCopyInsideTheTenantIsPointedAtTheTransitAddress(t *testing.T) {
	original := []byte(`apiVersion: v1
kind: Config
clusters:
- name: infra-cluster
  cluster:
    server: https://10.198.175.250:6443
    certificate-authority-data: Zm9v
`)

	out, err := throughTheTransitPlane(original, "10.199.0.100")
	if err != nil {
		t.Fatalf("rewriting: %v", err)
	}
	var parsed struct {
		Clusters []struct {
			Cluster struct {
				Server        string `json:"server"`
				ServerName    string `json:"tls-server-name"`
				CAData        string `json:"certificate-authority-data"`
				SkipTLSVerify bool   `json:"insecure-skip-tls-verify"`
			} `json:"cluster"`
		} `json:"clusters"`
	}
	if err := yaml.Unmarshal(out, &parsed); err != nil {
		t.Fatalf("reading it back: %v", err)
	}
	cluster := parsed.Clusters[0].Cluster
	if cluster.Server != "https://10.199.0.100:6444" {
		t.Errorf("server = %q", cluster.Server)
	}
	if cluster.ServerName != "kubernetes.default.svc" {
		t.Errorf("tls-server-name = %q — the VIP is in no certificate", cluster.ServerName)
	}
	if cluster.SkipTLSVerify {
		t.Error("verification was turned off instead of redirected")
	}
	if cluster.CAData != "Zm9v" {
		t.Errorf("the CA was lost: %q", cluster.CAData)
	}

	// A tenant with no VIP is left exactly as it was.
	same, err := throughTheTransitPlane(original, "")
	if err != nil || string(same) != string(original) {
		t.Errorf("a tenant on the default overlay was rewritten: %v %q", err, same)
	}
}
