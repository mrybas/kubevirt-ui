package controller

import (
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

func capiObject(gvk schema.GroupVersionKind, namespace, name string) (*unstructured.Unstructured, error) {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(gvk)
	err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: namespace, Name: name,
	}, obj)
	return obj, err
}

// TestAVPCTenantJoinsOnItsOwnAddress.
//
// The endpoint used to be the external ingress name, on the assumption that
// cluster-info already carried the address. It cannot: CAPI copies this field
// into the worker's discovery endpoint, and the worker has to fetch
// cluster-info before it can learn anything from it. So the worker dialled a
// name its VPC could neither resolve nor route to, and three lab runs read that
// as "CAPK will not bootstrap".
func TestAVPCTenantJoinsOnItsOwnAddress(t *testing.T) {
	mustTenant(t, vpcTalosTenant("tcp1"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("tcp1")
		return err
	})
	assignAddress(t, "tcp1", "10.199.0.111")

	eventually(t, "the Cluster", func() error {
		cluster, err := capiObject(clusterGVK, "tenant-tcp1", "tcp1")
		if err != nil {
			return err
		}
		host, _, _ := unstructured.NestedString(cluster.Object,
			"spec", "controlPlaneEndpoint", "host")
		port, _, _ := unstructured.NestedInt64(cluster.Object,
			"spec", "controlPlaneEndpoint", "port")
		if host != "10.199.0.111" || port != 6443 {
			return fmt.Errorf("endpoint = %s:%d", host, port)
		}
		apiPort, found, _ := unstructured.NestedInt64(cluster.Object,
			"spec", "clusterNetwork", "apiServerPort")
		if !found || apiPort != 6443 {
			return fmt.Errorf("apiServerPort = %d (found=%v) — it is what the "+
				"apiserver listens on and what cluster-info advertises",
				apiPort, found)
		}
		return nil
	})

	// The control plane advertises the same address, or cluster-info sends the
	// worker somewhere it cannot reach.
	cp, err := capiObject(kamajiControlPlaneGVK, "tenant-tcp1", "tcp1")
	if err != nil {
		t.Fatalf("reading the control plane: %v", err)
	}
	if advertise, _, _ := unstructured.NestedString(cp.Object,
		"spec", "network", "advertiseAddress"); advertise != "10.199.0.111" {
		t.Errorf("advertiseAddress = %q", advertise)
	}
	// And the port a Talos worker actually dials is published by the tenant's
	// own Service, not by the control plane object.
	//
	// The product writes `network.additionalPorts` for this, and the field does
	// not exist: KamajiControlPlane's schema has no such property, so the API
	// server prunes it without a word. Both live tenants carry exactly
	// advertiseAddress, certSANs and serviceType. It never mattered because the
	// Services publish 50001 — but a write that looks like configuration and is
	// not should not be copied into the operator.
	if _, found, _ := unstructured.NestedSlice(cp.Object,
		"spec", "network", "additionalPorts"); found {
		t.Error("it wrote a field the schema does not have, which the API " +
			"server drops in silence")
	}
	service := mustCPService(t, "tcp1")
	published := map[string]int32{}
	for _, port := range service.Spec.Ports {
		published[port.Name] = port.Port
	}
	if published["trustd"] != 50001 {
		t.Errorf("the tenant's own Service publishes %v — a Talos worker dials "+
			"trustd on 50001 of the host it was given, and the port is not "+
			"configurable", published)
	}
}

// TestADefaultOverlayTenantJoinsOnTheClusterIP.
//
// There the Kamaji Service's address is natively routable, and it does not
// exist when the Cluster is first written — so the tenant says what it is
// waiting for instead of writing an endpoint it has invented.
func TestADefaultOverlayTenantJoinsOnTheClusterIP(t *testing.T) {
	mustTenant(t, plainTenant("tcp2"))

	eventually(t, "the tenant to name what is missing", func() error {
		condition := tenantCondition(getTenant(t, "tcp2"),
			platformv1alpha1.ConditionControlPlaneReady)
		if condition == nil {
			return fmt.Errorf("no condition")
		}
		if condition.Reason != "WaitingForControlPlaneService" {
			return fmt.Errorf("reason = %q (%s)", condition.Reason, condition.Message)
		}
		return nil
	})
	if _, err := capiObject(clusterGVK, "tenant-tcp2", "tcp2"); err == nil {
		t.Error("a Cluster was written with an endpoint nobody has yet")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the Cluster: %v", err)
	}

	// Kamaji's part.
	service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{
		Namespace: "tenant-tcp2", Name: "tcp2",
	}}
	service.Spec.Ports = []corev1.ServicePort{{Name: "kube-apiserver", Port: 6443}}
	if err := k8sClient.Create(testCtx, service); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the control-plane Service: %v", err)
	}

	eventually(t, "the endpoint to follow it", func() error {
		cluster, err := capiObject(clusterGVK, "tenant-tcp2", "tcp2")
		if err != nil {
			return err
		}
		host, _, _ := unstructured.NestedString(cluster.Object,
			"spec", "controlPlaneEndpoint", "host")
		if host == "" || host != service.Spec.ClusterIP {
			return fmt.Errorf("endpoint host = %q, ClusterIP = %q",
				host, service.Spec.ClusterIP)
		}
		// No apiServerPort on the default overlay: the port is Kamaji's default
		// and writing one here would change what the apiserver listens on.
		if _, found, _ := unstructured.NestedInt64(cluster.Object,
			"spec", "clusterNetwork", "apiServerPort"); found {
			return fmt.Errorf("apiServerPort was set on the default overlay")
		}
		return nil
	})

	cp, err := capiObject(kamajiControlPlaneGVK, "tenant-tcp2", "tcp2")
	if err != nil {
		t.Fatalf("reading the control plane: %v", err)
	}
	if _, found, _ := unstructured.NestedString(cp.Object,
		"spec", "network", "advertiseAddress"); found {
		t.Error("it advertised an address on the default overlay, where the " +
			"ClusterIP is what workers use")
	}
}

// TestTheSignerRunsBesideTheApiserverUnderTheNameKamajiReads.
//
// KamajiControlPlane calls these `extraContainers`/`extraVolumes`.
// TenantControlPlane calls the same things `additionalContainers`/
// `additionalVolumes`, and writing those names here is not an error anyone
// reports: unknown fields are pruned silently, the object applies, the tenant
// comes up Ready — and the signer is simply not there, while the worker waits
// for a certificate nothing will ever issue.
func TestTheSignerRunsBesideTheApiserverUnderTheNameKamajiReads(t *testing.T) {
	mustTenant(t, vpcTalosTenant("tcp3"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("tcp3")
		return err
	})
	assignAddress(t, "tcp3", "10.199.0.112")

	eventually(t, "the control plane", func() error {
		_, err := capiObject(kamajiControlPlaneGVK, "tenant-tcp3", "tcp3")
		return err
	})
	cp, err := capiObject(kamajiControlPlaneGVK, "tenant-tcp3", "tcp3")
	if err != nil {
		t.Fatalf("reading the control plane: %v", err)
	}

	// Read back from the API server, so a field it pruned is a field that is
	// not there — which is the whole failure this guards.
	containers, found, _ := unstructured.NestedSlice(cp.Object,
		"spec", "deployment", "extraContainers")
	if !found || len(containers) != 1 {
		t.Fatalf("extraContainers = %v (found=%v)", containers, found)
	}
	container, _ := containers[0].(map[string]any)
	if container["name"] != "talos-csr-signer" {
		t.Errorf("container = %v", container["name"])
	}
	args := fmt.Sprint(container["args"])
	for _, flag := range []string{"--port=50001", "--ca-key-path"} {
		if !strings.Contains(args, flag) {
			t.Errorf("args = %s, missing %s — the CA key is what signs the CSR, "+
				"so mounting only the certificate leaves it able to issue nothing",
				args, flag)
		}
	}
	volumes, found, _ := unstructured.NestedSlice(cp.Object,
		"spec", "deployment", "extraVolumes")
	if !found || len(volumes) != 2 {
		t.Errorf("extraVolumes = %v", volumes)
	}

	// And the names the worker dials are in the certificate the apiserver
	// presents, or the join fails TLS before trustd is reached at all.
	sans, _, _ := unstructured.NestedStringSlice(cp.Object, "spec", "network", "certSANs")
	want := "tcp3-talos.tenant-tcp3.svc"
	if !strings.Contains(strings.Join(sans, ","), want) {
		t.Errorf("certSANs = %v, missing %s", sans, want)
	}
}

// TestTheMachineSecretsAreWrittenOnceAndNeverAgain.
//
// Every worker is derived from these values. Rotating the token means a new
// worker cannot authenticate to the signer while the existing ones stop being
// issued certificates — which presents as a broken signer rather than as a
// changed secret.
func TestTheMachineSecretsAreWrittenOnceAndNeverAgain(t *testing.T) {
	mustTenant(t, vpcTalosTenant("tcp4"))

	eventually(t, "the machine secrets", func() error {
		secret := &corev1.Secret{}
		return k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-tcp4", Name: "tcp4-talos-secrets",
		}, secret)
	})
	first := &corev1.Secret{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tcp4", Name: "tcp4-talos-secrets",
	}, first); err != nil {
		t.Fatalf("reading the secrets: %v", err)
	}
	for _, key := range []string{"machine.token", "cluster.id", "cluster.secret"} {
		if len(first.Data[key]) == 0 {
			t.Errorf("%s is empty", key)
		}
	}
	// kubeadm format: six characters, a dot, sixteen.
	token := string(first.Data["machine.token"])
	if parts := strings.Split(token, "."); len(parts) != 2 ||
		len(parts[0]) != 6 || len(parts[1]) != 16 {
		t.Errorf("machine.token = %q", token)
	}

	// Wake it repeatedly; the values must not move.
	for poke := 1; poke <= 3; poke++ {
		assignAddress(t, "tcp4", fmt.Sprintf("10.199.0.11%d", poke+2))
	}
	consistently(t, "the secrets to stay as they were", 5*time.Second, func() error {
		live := &corev1.Secret{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-tcp4", Name: "tcp4-talos-secrets",
		}, live); err != nil {
			return err
		}
		if string(live.Data["machine.token"]) != token {
			return fmt.Errorf("the token was rotated under a running tenant")
		}
		if live.ResourceVersion != first.ResourceVersion {
			return fmt.Errorf("the secret was rewritten: %s -> %s",
				first.ResourceVersion, live.ResourceVersion)
		}
		return nil
	})
}

// TestTheControlPlaneConditionFollowsCAPIRatherThanOurOwnWrite.
//
// Writing a KamajiControlPlane always succeeds. Whether an apiserver came up
// behind it is a different question, and the only one a worker cares about — so
// the condition is read from the Cluster's own status, and it has to arrive
// through the manager rather than on a resync twelve hours later.
func TestTheControlPlaneConditionFollowsCAPIRatherThanOurOwnWrite(t *testing.T) {
	mustTenant(t, vpcTenant("tcp5"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("tcp5")
		return err
	})
	assignAddress(t, "tcp5", "10.199.0.118")

	eventually(t, "the tenant to report it as provisioning", func() error {
		condition := tenantCondition(getTenant(t, "tcp5"),
			platformv1alpha1.ConditionControlPlaneReady)
		if condition == nil {
			return fmt.Errorf("no condition")
		}
		if condition.Status != metav1.ConditionFalse || condition.Reason != "Provisioning" {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})

	// CAPI's part.
	cluster, err := capiObject(clusterGVK, "tenant-tcp5", "tcp5")
	if err != nil {
		t.Fatalf("reading the Cluster: %v", err)
	}
	if err := unstructured.SetNestedField(cluster.Object, true,
		"status", "controlPlaneReady"); err != nil {
		t.Fatal(err)
	}
	if err := k8sClient.Status().Update(testCtx, cluster); err != nil {
		t.Fatalf("marking the control plane ready: %v", err)
	}

	eventually(t, "the tenant to notice", func() error {
		condition := tenantCondition(getTenant(t, "tcp5"),
			platformv1alpha1.ConditionControlPlaneReady)
		if condition == nil || condition.Status != metav1.ConditionTrue {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})

	// And back again: nothing requeues a tenant that is already ready, so the
	// falling edge is the watch's alone.
	cluster, err = capiObject(clusterGVK, "tenant-tcp5", "tcp5")
	if err != nil {
		t.Fatalf("reading the Cluster: %v", err)
	}
	if err := unstructured.SetNestedField(cluster.Object, false,
		"status", "controlPlaneReady"); err != nil {
		t.Fatal(err)
	}
	if err := k8sClient.Status().Update(testCtx, cluster); err != nil {
		t.Fatalf("marking the control plane down: %v", err)
	}
	eventually(t, "the tenant to stop claiming it is ready", func() error {
		condition := tenantCondition(getTenant(t, "tcp5"),
			platformv1alpha1.ConditionControlPlaneReady)
		if condition == nil || condition.Status != metav1.ConditionFalse {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})
}

// TestTheApiserverTakesTheIdentityProviderOnlyWhenAsked.
//
// Off means no `--oidc-*` flags at all, which is what a deployment whose
// provider the apiserver cannot reach — or whose certificate it does not trust
// — actually needs. Found missing by diffing against a live tenant, which
// carries four of them.
func TestTheApiserverTakesTheIdentityProviderOnlyWhenAsked(t *testing.T) {
	t.Setenv("OIDC_ISSUER", "https://example.invalid/dex")
	t.Setenv("OIDC_CLIENT_ID", "kubevirt-ui")

	off := vpcTenant("tcp6")
	if args, refusal := oidcArgs(off); args != nil || refusal != "" {
		t.Errorf("a tenant that did not ask for it got %v / %q", args, refusal)
	}

	on := vpcTenant("tcp7")
	on.Spec.EnableOIDC = true
	args, refusal := oidcArgs(on)
	if refusal != "" {
		t.Fatalf("refused a request it can satisfy: %s", refusal)
	}
	if len(args) != 4 {
		t.Fatalf("args = %v", args)
	}
	if fmt.Sprint(args[0]) != "--oidc-issuer-url=https://example.invalid/dex" {
		t.Errorf("args = %v", args)
	}

	// An apiserver told to trust an http issuer refuses to start, and a control
	// plane that will not start is a worse answer than one without single
	// sign-on. It is said out loud rather than dropped, which is the whole of
	// UAT run 4's O-1.
	t.Setenv("OIDC_ISSUER", "http://example.invalid/dex")
	args, refusal = oidcArgs(on)
	if args != nil {
		t.Errorf("an http issuer was accepted: %v", args)
	}
	if !strings.Contains(refusal, "https") {
		t.Errorf("the refusal does not say what is wrong: %q", refusal)
	}
}

// TestATenantThatAskedForSingleSignOnAndDidNotGetItSaysSo.
//
// UAT run 4, O-1: `enableOIDC: true` on two tenants, wizard showing "Enabled",
// no `--oidc-*` argument anywhere on either apiserver, and every condition
// True. The deployment simply had no OIDC_ISSUER, and the code returned nil
// without a word — so an OIDC kubeconfig for those tenants could only answer
// Unauthorized, with nothing anywhere to explain it.
func TestATenantThatAskedForSingleSignOnAndDidNotGetItSaysSo(t *testing.T) {
	asked := vpcTenant("tcp8")
	asked.Spec.EnableOIDC = true

	t.Setenv("OIDC_ISSUER", "")
	cond := singleSignOnCondition(asked)
	if cond.Status != metav1.ConditionFalse {
		t.Fatalf("condition = %+v", cond)
	}
	// The missing precondition by name, and what to do about it.
	for _, want := range []string{"OIDC_ISSUER", "oidcIssuer"} {
		if !strings.Contains(cond.Message, want) {
			t.Errorf("message does not name %q: %s", want, cond.Message)
		}
	}
	// And what the tenant *is*, since it was built anyway.
	if !strings.Contains(cond.Message, "without single sign-on") {
		t.Errorf("message does not say what was built: %s", cond.Message)
	}

	t.Setenv("OIDC_ISSUER", "https://example.invalid/dex")
	if cond := singleSignOnCondition(asked); cond.Status != metav1.ConditionTrue {
		t.Errorf("condition with an issuer configured = %+v", cond)
	}

	notAsked := vpcTenant("tcp9")
	cond = singleSignOnCondition(notAsked)
	if cond.Status != metav1.ConditionTrue || cond.Reason != "NotRequested" {
		t.Errorf("a tenant that did not ask reads as broken: %+v", cond)
	}
}

// TestTheControlPlaneKamajiWroteAboutItselfSurvives.
//
// Kamaji writes five fields of its own into this object — the endpoint it
// settled on, kine, the registry, the controller manager, the scheduler — and
// none is rendered here. Replacing the spec strips them, Kamaji writes them
// back, and a live control plane is rewritten on every pass for no reason.
//
// Found by predicting an adoption against the stand: the live object carries
// thirteen spec fields and this renders eight.
func TestTheControlPlaneKamajiWroteAboutItselfSurvives(t *testing.T) {
	// Paused, so the only writer in this test is the call under test. The
	// running manager reconciling the same tenant would be a second one, and
	// "resourceVersion did not move" cannot be asserted with two.
	obj := vpcTalosTenant("tcp8")
	obj.Annotations = map[string]string{"platform.kubevirt-ui.io/paused": "true"}
	mustTenant(t, obj)
	mustNamespace(t, "tenant-tcp8", "")

	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
	}
	if err := reconciler.ensureKamajiControlPlane(
		testCtx, obj, "tenant-tcp8", "10.199.0.119", true); err != nil {
		t.Fatalf("first pass: %v", err)
	}

	// Kamaji's part.
	cp, err := capiObject(kamajiControlPlaneGVK, "tenant-tcp8", "tcp8")
	if err != nil {
		t.Fatalf("reading the control plane: %v", err)
	}
	_ = unstructured.SetNestedMap(cp.Object, map[string]any{
		"host": "10.103.184.143", "port": int64(6443),
	}, "spec", "controlPlaneEndpoint")
	// `kine.extraArgs`, because `kine.image` is not in the schema and the API
	// server prunes it — which the first version of this test discovered by
	// asserting on a field that had never been stored.
	_ = unstructured.SetNestedStringSlice(cp.Object,
		[]string{"--metrics-bind-address=0"}, "spec", "kine", "extraArgs")
	if err := k8sClient.Update(testCtx, cp); err != nil {
		t.Fatalf("writing Kamaji's own fields: %v", err)
	}
	settled := cp.GetResourceVersion()

	for pass := 1; pass <= 3; pass++ {
		if err := reconciler.ensureKamajiControlPlane(
			testCtx, obj, "tenant-tcp8", "10.199.0.119", true); err != nil {
			t.Fatalf("pass %d: %v", pass, err)
		}
	}

	after, err := capiObject(kamajiControlPlaneGVK, "tenant-tcp8", "tcp8")
	if err != nil {
		t.Fatalf("reading it back: %v", err)
	}
	if host, _, _ := unstructured.NestedString(after.Object,
		"spec", "controlPlaneEndpoint", "host"); host != "10.103.184.143" {
		t.Errorf("it stripped the endpoint Kamaji settled on: %q", host)
	}
	if args, _, _ := unstructured.NestedStringSlice(after.Object,
		"spec", "kine", "extraArgs"); len(args) != 1 {
		t.Errorf("it stripped a defaulted field: %v", args)
	}
	if after.GetResourceVersion() != settled {
		t.Errorf("three passes moved resourceVersion %s -> %s over an object "+
			"nothing asked it to change", settled, after.GetResourceVersion())
	}
}

// TestAdoptionDoesNotClearMetadataSomebodyElseWrote.
//
// Measured on the stand, by adopting a live tenant: writing the Cluster
// stripped `kubevirt-ui.io/worker-type` and `kubevirt-ui.io/enable-oidc` from
// it. Both were written by the product, both are carried by every tenant beside
// it, and both were gone the moment this operator touched the object — because
// the writer replaced the annotation map instead of merging into it.
//
// Metadata somebody else put there is not this writer's to clear just because
// it does not render it.
func TestAdoptionDoesNotClearMetadataSomebodyElseWrote(t *testing.T) {
	obj := vpcTalosTenant("tcp9")
	obj.Annotations = map[string]string{"platform.kubevirt-ui.io/paused": "true"}
	mustTenant(t, obj)
	mustNamespace(t, "tenant-tcp9", "")

	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
	}
	if err := reconciler.ensureCluster(
		testCtx, obj, "tenant-tcp9", "10.199.0.120", 6443, true); err != nil {
		t.Fatalf("first pass: %v", err)
	}

	// The product's part.
	cluster, err := capiObject(clusterGVK, "tenant-tcp9", "tcp9")
	if err != nil {
		t.Fatalf("reading the Cluster: %v", err)
	}
	annotations := cluster.GetAnnotations()
	annotations["kubevirt-ui.io/worker-type"] = "vm"
	annotations["kubevirt-ui.io/enable-oidc"] = "true"
	cluster.SetAnnotations(annotations)
	labels := cluster.GetLabels()
	labels["somebody-elses"] = "label"
	cluster.SetLabels(labels)
	if err := k8sClient.Update(testCtx, cluster); err != nil {
		t.Fatalf("writing the product's metadata: %v", err)
	}

	if err := reconciler.ensureCluster(
		testCtx, obj, "tenant-tcp9", "10.199.0.120", 6443, true); err != nil {
		t.Fatalf("second pass: %v", err)
	}

	after, err := capiObject(clusterGVK, "tenant-tcp9", "tcp9")
	if err != nil {
		t.Fatalf("reading it back: %v", err)
	}
	for key, want := range map[string]string{
		"kubevirt-ui.io/worker-type":  "vm",
		"kubevirt-ui.io/enable-oidc":  "true",
		"kubevirt-ui.io/display-name": "tcp9",
	} {
		if got := after.GetAnnotations()[key]; got != want {
			t.Errorf("%s = %q, want %q", key, got, want)
		}
	}
	if after.GetLabels()["somebody-elses"] != "label" {
		t.Errorf("labels = %v", after.GetLabels())
	}
}

// TestATenantWithStorageSaysSoOnItsCluster.
//
// The product's gate on "may this tenant install the CSI driver" is
// `KubevirtCluster.spec.infraClusterSecretRef`, and the operator never wrote
// it. So a tenant the operator built could not have storage enabled from the
// UI: the button answered "this tenant was created without storage — recreate
// it with storage enabled" for a tenant whose host side was right there.
//
// Read from the credential rather than from a field on the description: the
// host side of storage is the product's to create, and its existence is the
// fact.
func TestATenantWithStorageSaysSoOnItsCluster(t *testing.T) {
	mustNamespace(t, "tenant-tcs1", "")
	obj := talosTenant("tcs1")
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
	}

	// No credential yet: nothing to point at, and saying otherwise would be a
	// reference to a secret that is not there.
	if err := reconciler.ensureKubevirtCluster(testCtx, obj, "tenant-tcs1"); err != nil {
		t.Fatalf("without storage: %v", err)
	}
	cluster := &unstructured.Unstructured{}
	cluster.SetGroupVersionKind(kubevirtClusterGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tcs1", Name: "tcs1"}, cluster); err != nil {
		t.Fatalf("reading the cluster: %v", err)
	}
	if _, found, _ := unstructured.NestedMap(cluster.Object,
		"spec", "infraClusterSecretRef"); found {
		t.Error("it claimed storage with no credential to point at")
	}

	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: "tenant-tcs1", Name: capkCredentialSecret}}
	if err := k8sClient.Create(testCtx, secret); err != nil {
		t.Fatalf("creating the credential: %v", err)
	}

	if err := reconciler.ensureKubevirtCluster(testCtx, obj, "tenant-tcs1"); err != nil {
		t.Fatalf("with storage: %v", err)
	}
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tcs1", Name: "tcs1"}, cluster); err != nil {
		t.Fatalf("re-reading the cluster: %v", err)
	}
	ref, found, _ := unstructured.NestedMap(cluster.Object,
		"spec", "infraClusterSecretRef")
	if !found {
		t.Fatal("the credential exists and the cluster does not say so")
	}
	if ref["name"] != capkCredentialSecret || ref["namespace"] != "tenant-tcs1" {
		t.Errorf("ref = %v", ref)
	}
}
