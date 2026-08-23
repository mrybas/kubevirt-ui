/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"fmt"
	"net/netip"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/acl"
	"github.com/mrybas/kubevirt-ui/operator/internal/metrics"
	"github.com/mrybas/kubevirt-ui/operator/internal/network"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

// liveVPCObject reads the kube-ovn Vpc a network wrote.
func liveVPCObject(name string) (*unstructured.Unstructured, error) {
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	err := k8sReader.Get(testCtx, types.NamespacedName{Name: name}, vpc)
	return vpc, err
}

func mustNetwork(t *testing.T, net *platformv1alpha1.ManagedNetwork) *platformv1alpha1.ManagedNetwork {
	t.Helper()
	if err := k8sClient.Create(testCtx, net); err != nil {
		t.Fatalf("creating network %s: %v", net.Name, err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, net) })
	return net
}

func getNetwork(t *testing.T, name string) *platformv1alpha1.ManagedNetwork {
	t.Helper()
	out := &platformv1alpha1.ManagedNetwork{}
	var last error
	for i := 0; i < 50; i++ {
		last = k8sClient.Get(testCtx, types.NamespacedName{Name: name}, out)
		if last == nil {
			return out
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("reading network %s: %v", name, last)
	return nil
}

// mustExternalSubnet is the egress plane the next hop is read from.
func mustExternalSubnet(t *testing.T, name, cidr, gateway string) {
	t.Helper()
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	subnet.SetName(name)
	_ = unstructured.SetNestedMap(subnet.Object, map[string]any{
		"protocol": "IPv4", "cidrBlock": cidr, "gateway": gateway,
	}, "spec")
	if err := k8sClient.Create(testCtx, subnet); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the external subnet %s: %v", name, err)
	}
}

// poke wakes the controller without changing anything it renders.
//
// Retried on conflict: the controller writes status on its own schedule, and a
// test that loses that race is reporting on the test, not on the controller.
func poke(t *testing.T, name, value string) {
	t.Helper()
	var last error
	for i := 0; i < 20; i++ {
		current := getNetwork(t, name)
		patched := current.DeepCopy()
		if patched.Annotations == nil {
			patched.Annotations = map[string]string{}
		}
		patched.Annotations["test.kubevirt-ui.io/poke"] = value
		last = k8sClient.Patch(testCtx, patched, client.MergeFrom(current))
		if last == nil {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("poking %s: %v", name, last)
}

// mustKubeOVNNamespace tolerates the namespace already being there: several
// tests share the one the controller is pinned to.
func mustSharedNamespace(t *testing.T, name string) {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating namespace %s: %v", name, err)
	}
}

// mustOverlaySubnet is the default cluster overlay; its gateway is the next hop
// the service route uses.
func mustOverlaySubnet(t *testing.T) {
	t.Helper()
	mustExternalSubnet(t, overlaySubnet, "10.16.0.0/16", "10.16.0.1")
}

func networkCondition(net *platformv1alpha1.ManagedNetwork, kind string) *metav1.Condition {
	return apimeta.FindStatusCondition(net.Status.Conditions, kind)
}

// readVPC returns an error rather than failing the test: it is called from
// inside eventually(), where the first miss is the normal case and a Fatalf
// turns "not yet" into "never".
func readVPC(name string) (*unstructured.Unstructured, error) {
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, vpc); err != nil {
		return nil, err
	}
	return vpc, nil
}

func liveVPC(t *testing.T, name string) *unstructured.Unstructured {
	t.Helper()
	vpc, err := readVPC(name)
	if err != nil {
		t.Fatalf("reading Vpc/%s: %v", name, err)
	}
	return vpc
}

// TestNetworkBuildsTheVPCAndItsSubnet is the base case, and it checks the one
// thing that is easy to get half-right: the master switch and the attachment
// array, which do nothing apart.
func TestNetworkBuildsTheVPCAndItsSubnet(t *testing.T) {
	mustExternalSubnet(t, "net-external", "10.199.4.0/22", "10.199.4.254")
	mustExternalSubnet(t, "net-transit", "10.199.0.0/22", "10.199.0.1")

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netbuild"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:        "10.200.100.0/22",
			Folder:      "poc",
			Environment: "dev",
			Tenant:      "t9",
			DNSServer:   "10.96.0.200",
			ExternalPlane: &platformv1alpha1.ExternalPlane{
				Attachments:  []string{"net-transit"},
				EgressSubnet: "net-external",
			},
		},
	})

	eventually(t, "the VPC", func() error {
		vpc, err := readVPC("netbuild")
		if err != nil {
			return err
		}
		enabled, _, _ := unstructured.NestedBool(vpc.Object, "spec", "enableExternal")
		attached, _, _ := unstructured.NestedStringSlice(vpc.Object, "spec", "extraExternalSubnets")
		if !enabled {
			return fmt.Errorf("enableExternal is not set; kube-ovn will not read the array")
		}
		if len(attached) != 2 || attached[0] != "net-transit" || attached[1] != "net-external" {
			return fmt.Errorf("extraExternalSubnets = %v", attached)
		}
		routes, _, _ := unstructured.NestedSlice(vpc.Object, "spec", "staticRoutes")
		for _, raw := range routes {
			route, _ := raw.(map[string]any)
			if route["cidr"] == "0.0.0.0/0" {
				// Read from the egress Subnet, not configured: the same number
				// in two places is the same number until one of them changes.
				if route["nextHopIP"] != "10.199.4.254" {
					return fmt.Errorf("next hop = %v", route["nextHopIP"])
				}
				return nil
			}
		}
		return fmt.Errorf("no default route in %v", routes)
	})

	eventually(t, "the default subnet", func() error {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netbuild-default"}, subnet); err != nil {
			return err
		}
		spec, _, _ := unstructured.NestedMap(subnet.Object, "spec")
		if spec["cidrBlock"] != "10.200.100.0/22" || spec["gateway"] != "10.200.100.1" {
			return fmt.Errorf("addressing = %v / %v", spec["cidrBlock"], spec["gateway"])
		}
		// Without this a namespace joining the VPC lands on the cluster overlay
		// instead of the VPC subnet.
		if spec["default"] != true {
			return fmt.Errorf("default = %v", spec["default"])
		}
		if spec["dhcpV4Options"] != "lease_time=3600,router=10.200.100.1,"+
			"server_id=10.200.100.1,dns_server=10.96.0.200" {
			return fmt.Errorf("dhcpV4Options = %v", spec["dhcpV4Options"])
		}
		if spec["vpc"] != "netbuild" {
			return fmt.Errorf("vpc = %v", spec["vpc"])
		}
		labels := subnet.GetLabels()
		if labels[network.FolderLabel] != "poc" || labels[network.TenantLabel] != "t9" {
			return fmt.Errorf("labels = %v", labels)
		}
		return nil
	})

	eventually(t, "both conditions", func() error {
		net := getNetwork(t, "netbuild")
		for _, kind := range []string{
			platformv1alpha1.ConditionNetworkReady, platformv1alpha1.ConditionAttached,
		} {
			cond := networkCondition(net, kind)
			if cond == nil || cond.Status != metav1.ConditionTrue {
				return fmt.Errorf("%s = %v", kind, cond)
			}
		}
		if net.Status.DefaultRouteVia != "10.199.4.254" {
			return fmt.Errorf("status.defaultRouteVia = %q", net.Status.DefaultRouteVia)
		}
		return nil
	})
}

// TestIsolationIsRecordedByAbsence: the opt-out annotation is written only for
// "no", so that silence cannot be read as consent to stay open — which is
// exactly how the old default behaved.
func TestIsolationIsRecordedByAbsence(t *testing.T) {
	open := false
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netopen"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR: "10.200.104.0/22", Isolated: &open,
		},
	})
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netclosed"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.108.0/22"},
	})

	annotationOf := func(name string) (string, bool, error) {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, subnet); err != nil {
			return "", false, err
		}
		value, present := subnet.GetAnnotations()[network.IsolationOptOutAnnotation]
		return value, present, nil
	}

	eventually(t, "the opt-out to be recorded for the open network", func() error {
		value, present, err := annotationOf("netopen-default")
		if err != nil {
			return err
		}
		if !present || value != network.IsolationOptOutValue {
			return fmt.Errorf("annotation = %q present=%v", value, present)
		}
		return nil
	})

	eventually(t, "no opt-out on the isolated network", func() error {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netclosed-default"}, subnet); err != nil {
			return err
		}
		if _, present := subnet.GetAnnotations()[network.IsolationOptOutAnnotation]; present {
			return fmt.Errorf("an isolated network recorded an opt-out")
		}
		return nil
	})

	// And changing the answer back must remove it: a stale opt-out is a network
	// that silently stopped being isolated.
	current := getNetwork(t, "netopen")
	patched := current.DeepCopy()
	closed := true
	patched.Spec.Isolated = &closed
	if err := k8sClient.Update(testCtx, patched); err != nil {
		t.Fatalf("closing the network: %v", err)
	}
	eventually(t, "the opt-out to be withdrawn", func() error {
		_, present, err := annotationOf("netopen-default")
		if err != nil {
			return err
		}
		if present {
			return fmt.Errorf("opt-out still there after the decision changed")
		}
		return nil
	})
}

// TestNetworkNeverTouchesACLs is the whole reason this slice stops where it
// does. `Subnet.spec.acls` has one writer — the isolation reconciler in the
// backend — and two writers of one list is the failure the operator exists to
// remove.
func TestNetworkNeverTouchesACLs(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netacl"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.112.0/22"},
	})

	eventually(t, "the subnet", func() error {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		return k8sClient.Get(testCtx, types.NamespacedName{Name: "netacl-default"}, subnet)
	})

	// Somebody else's rules, written the way the backend writes them.
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netacl-default"}, subnet); err != nil {
		t.Fatalf("reading the subnet: %v", err)
	}
	if err := unstructured.SetNestedSlice(subnet.Object, []any{
		map[string]any{
			"action": "drop", "direction": "to-lport",
			"match": "ip4.src == 10.200.0.0/14", "priority": int64(3000),
		},
	}, "spec", "acls"); err != nil {
		t.Fatalf("building the ACL: %v", err)
	}
	if err := k8sClient.Update(testCtx, subnet); err != nil {
		t.Fatalf("writing somebody else's ACLs: %v", err)
	}

	// Poke the controller so this is a statement about reconciles, not about
	// the controller having been asleep.
	poke(t, "netacl", "1")

	consistently(t, "the other writer's ACLs surviving", 3*time.Second, func() error {
		live := &unstructured.Unstructured{}
		live.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netacl-default"}, live); err != nil {
			return err
		}
		acls, _, _ := unstructured.NestedSlice(live.Object, "spec", "acls")
		if len(acls) != 1 {
			return fmt.Errorf("acls = %v", acls)
		}
		return nil
	})
}

// TestNetworkLeavesAnotherWritersRoutesAlone: peering writes into the same
// staticRoutes list, so replacing it would delete that writer's work on the
// first pass.
func TestNetworkLeavesAnotherWritersRoutesAlone(t *testing.T) {
	mustExternalSubnet(t, "keep-external", "10.199.8.0/22", "10.199.8.254")

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netkeep"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR: "10.200.116.0/22",
			ExternalPlane: &platformv1alpha1.ExternalPlane{
				EgressSubnet: "keep-external",
			},
		},
	})

	eventually(t, "the default route", func() error {
		vpc, err := readVPC("netkeep")
		if err != nil {
			return err
		}
		routes, _, _ := unstructured.NestedSlice(vpc.Object, "spec", "staticRoutes")
		if len(routes) != 1 {
			return fmt.Errorf("routes = %v", routes)
		}
		return nil
	})

	// A peering route, as the peering path writes it.
	vpc := liveVPC(t, "netkeep")
	routes, _, _ := unstructured.NestedSlice(vpc.Object, "spec", "staticRoutes")
	routes = append(routes, map[string]any{
		"cidr": "10.200.120.0/22", "nextHopIP": "10.201.0.2", "policy": "policyDst",
	})
	if err := unstructured.SetNestedSlice(vpc.Object, routes, "spec", "staticRoutes"); err != nil {
		t.Fatalf("building the peering route: %v", err)
	}
	if err := k8sClient.Update(testCtx, vpc); err != nil {
		t.Fatalf("writing the peering route: %v", err)
	}

	poke(t, "netkeep", "1")

	consistently(t, "both routes surviving", 3*time.Second, func() error {
		vpc, err := readVPC("netkeep")
		if err != nil {
			return err
		}
		live, _, _ := unstructured.NestedSlice(vpc.Object, "spec", "staticRoutes")
		if len(live) != 2 {
			return fmt.Errorf("routes = %v", live)
		}
		return nil
	})
}

func networkWrites() float64 {
	var sum float64
	for _, kind := range []string{"Vpc", "Subnet"} {
		for _, op := range []string{"created", "updated"} {
			sum += testutil.ToFloat64(
				metrics.PatchesTotal.WithLabelValues(kind, networkControllerName, op))
		}
	}
	return sum
}

// TestNetworkStopsWriting is the write-on-diff rule on objects that default
// themselves. kube-ovn stores three keys per static route that nothing here
// sets; comparing whole routes would rewrite the list on every pass, and
// resourceVersion would not move to show it.
func TestNetworkStopsWriting(t *testing.T) {
	mustExternalSubnet(t, "quiet-external", "10.199.12.0/22", "10.199.12.254")

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netquiet"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:      "10.200.124.0/22",
			DNSServer: "10.96.0.200",
			ExternalPlane: &platformv1alpha1.ExternalPlane{
				Attachments:  []string{"quiet-external"},
				EgressSubnet: "quiet-external",
			},
		},
	})

	eventually(t, "the network to settle", func() error {
		net := getNetwork(t, "netquiet")
		cond := networkCondition(net, platformv1alpha1.ConditionNetworkReady)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("not ready yet: %v", cond)
		}
		first := networkWrites()
		time.Sleep(500 * time.Millisecond)
		if networkWrites() != first {
			return fmt.Errorf("still settling")
		}
		return nil
	})
	baseline := networkWrites()

	for i := 0; i < 5; i++ {
		poke(t, "netquiet", fmt.Sprintf("%d", i))
	}

	consistently(t, "no further writes", 3*time.Second, func() error {
		if now := networkWrites(); now != baseline {
			return fmt.Errorf("writes went from %v to %v with nothing changed", baseline, now)
		}
		return nil
	})
}

// TestTheServiceRouteGoesOnWhenTheDeploymentAppears is the defect this slice
// exists for.
//
// kube-ovn creates the VpcDns Deployment *after* the VpcDns object, so at
// create time there is nothing to annotate. The old path tried once, logged
// that it would be applied "on the next reconcile", and had no next reconcile —
// only a person calling the recreate endpoint. Without the route a VpcDns pod
// cannot reach the cluster resolver's ClusterIP at all, and the object reports
// ACTIVE with its pods Running the whole time.
func TestTheServiceRouteGoesOnWhenTheDeploymentAppears(t *testing.T) {
	mustSharedNamespace(t, "kube-ovn")
	mustOverlaySubnet(t)
	// The feature has to be on for any of this to be reachable — see
	// TestAVpcDnsNobodyServesIsNotCalledPending.
	mustKubeOVNResolver(t)

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netdns"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:        "10.200.128.0/22",
			ServiceCIDR: "10.96.0.0/12",
		},
	})

	eventually(t, "the VpcDns object", func() error {
		dns := &unstructured.Unstructured{}
		dns.SetGroupVersionKind(vpcDNSGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netdns-dns"}, dns); err != nil {
			return err
		}
		vpc, _, _ := unstructured.NestedString(dns.Object, "spec", "vpc")
		subnet, _, _ := unstructured.NestedString(dns.Object, "spec", "subnet")
		if vpc != "netdns" || subnet != "netdns-default" {
			return fmt.Errorf("spec = %s / %s", vpc, subnet)
		}
		return nil
	})

	// Before the Deployment exists this is a waiting state, and it is reported
	// as one rather than passed over in silence.
	eventually(t, "the wait to be visible", func() error {
		cond := networkCondition(getNetwork(t, "netdns"), platformv1alpha1.ConditionDNSReady)
		if cond == nil || cond.Reason != "DeploymentPending" {
			return fmt.Errorf("condition = %v", cond)
		}
		return nil
	})

	// Now kube-ovn's controller does its part, some time later.
	deployment := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "vpc-dns-netdns-dns", Namespace: "kube-ovn"},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "vpc-dns"}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "vpc-dns"}},
				Spec: corev1.PodSpec{Containers: []corev1.Container{{
					Name: "coredns", Image: "coredns/coredns:1.11.1",
				}}},
			},
		},
	}
	if err := k8sClient.Create(testCtx, deployment); err != nil {
		t.Fatalf("creating the VpcDns deployment: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, deployment) })

	eventually(t, "the route to be applied without anybody asking", func() error {
		live := &appsv1.Deployment{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kube-ovn", Name: "vpc-dns-netdns-dns",
		}, live); err != nil {
			return err
		}
		got := live.Spec.Template.Annotations[serviceRouteAnnotation]
		want := `[{"dst":"10.96.0.0/12","gw":"10.16.0.1"}]`
		if got != want {
			return fmt.Errorf("annotation = %q, want %q", got, want)
		}
		return nil
	})

	eventually(t, "the route to be recorded", func() error {
		net := getNetwork(t, "netdns")
		if net.Status.ServiceRoute != "10.96.0.0/12 via 10.16.0.1" {
			return fmt.Errorf("status.serviceRoute = %q", net.Status.ServiceRoute)
		}
		// Not DNSReady=True: this envtest has no Kyverno, and without it
		// nothing injects the resolver into a pod, so a guest in this network
		// would still resolve at the cluster CoreDNS it cannot reach. That is
		// reported as its own reason and covered by
		// TestTheGroundUnderAVpcDnsIsBuilt; this test is about the route.
		cond := networkCondition(net, platformv1alpha1.ConditionDNSReady)
		if cond == nil {
			return fmt.Errorf("no DNS condition at all")
		}
		if cond.Reason == "WriteFailed" || cond.Reason == "DeploymentPending" {
			return fmt.Errorf("condition = %v", cond)
		}
		return nil
	})

	// And it is put back. Something else editing the pod template — a helm
	// upgrade of kube-ovn, a person — used to mean the route was gone until
	// somebody thought to call an endpoint.
	live := &appsv1.Deployment{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kube-ovn", Name: "vpc-dns-netdns-dns",
	}, live); err != nil {
		t.Fatalf("reading the deployment: %v", err)
	}
	// Tampered rather than deleted, deliberately. A missing annotation and a
	// wrong one are different code paths, and the wrong one is the dangerous
	// shape: the route is present, so anything checking for presence is
	// satisfied, and the packets still go nowhere.
	tampered := live.DeepCopy()
	tampered.Spec.Template.Annotations[serviceRouteAnnotation] =
		`[{"dst":"10.96.0.0/12","gw":"10.16.0.99"}]`
	if err := k8sClient.Patch(testCtx, tampered, client.MergeFrom(live)); err != nil {
		t.Fatalf("tampering with the route: %v", err)
	}

	eventually(t, "the wrong route to be corrected on its own", func() error {
		current := &appsv1.Deployment{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kube-ovn", Name: "vpc-dns-netdns-dns",
		}, current); err != nil {
			return err
		}
		got := current.Spec.Template.Annotations[serviceRouteAnnotation]
		if got != `[{"dst":"10.96.0.0/12","gw":"10.16.0.1"}]` {
			return fmt.Errorf("still %q", got)
		}
		return nil
	})

	// And the same for it being removed outright.
	current := &appsv1.Deployment{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kube-ovn", Name: "vpc-dns-netdns-dns",
	}, current); err != nil {
		t.Fatalf("reading the deployment: %v", err)
	}
	stripped := current.DeepCopy()
	delete(stripped.Spec.Template.Annotations, serviceRouteAnnotation)
	if err := k8sClient.Patch(testCtx, stripped, client.MergeFrom(current)); err != nil {
		t.Fatalf("stripping the route: %v", err)
	}

	eventually(t, "the missing route to come back on its own", func() error {
		after := &appsv1.Deployment{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kube-ovn", Name: "vpc-dns-netdns-dns",
		}, after); err != nil {
			return err
		}
		if after.Spec.Template.Annotations[serviceRouteAnnotation] == "" {
			return fmt.Errorf("still missing")
		}
		return nil
	})
}

// TestServiceCIDRIsConfigurableWhenItCannotBeDiscovered: on a managed control
// plane there is no kubeadm ConfigMap and no readable apiserver pod, so the
// cluster genuinely cannot be asked. That is a configuration case, and it has
// to say so rather than leaving DNS quietly unrouted.
func TestServiceCIDRIsConfigurableWhenItCannotBeDiscovered(t *testing.T) {
	mustSharedNamespace(t, "kube-ovn")
	mustOverlaySubnet(t)

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netnocidr"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.132.0/22"},
	})

	deployment := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "vpc-dns-netnocidr-dns", Namespace: "kube-ovn"},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "vpc-dns"}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "vpc-dns"}},
				Spec: corev1.PodSpec{Containers: []corev1.Container{{
					Name: "coredns", Image: "coredns/coredns:1.11.1",
				}}},
			},
		},
	}
	if err := k8sClient.Create(testCtx, deployment); err != nil {
		t.Fatalf("creating the VpcDns deployment: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, deployment) })

	// envtest has no apiserver pod and no kubeadm ConfigMap, which is exactly
	// the shape of a managed control plane.
	eventually(t, "an honest refusal naming both ways out", func() error {
		cond := networkCondition(getNetwork(t, "netnocidr"), platformv1alpha1.ConditionDNSReady)
		if cond == nil || cond.Reason != "ServiceCIDRUnknown" {
			return fmt.Errorf("condition = %v", cond)
		}
		for _, phrase := range []string{"--service-cidr", "spec.serviceCIDR", "managed control plane"} {
			if !strings.Contains(cond.Message, phrase) {
				return fmt.Errorf("message does not mention %q: %s", phrase, cond.Message)
			}
		}
		return nil
	})
}

// TestRetainIsTheDefaultAndDeletingTheCRChangesNothing is the property that
// makes adoption safe, and the one that saved a live network on this stand when
// an adoption CR had to be withdrawn in a hurry.
func TestRetainIsTheDefaultAndDeletingTheCRChangesNothing(t *testing.T) {
	net := mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netretain"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.136.0/22"},
	})

	eventually(t, "the VPC and subnet", func() error {
		if _, err := readVPC("netretain"); err != nil {
			return err
		}
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		return k8sClient.Get(testCtx, types.NamespacedName{Name: "netretain-default"}, subnet)
	})

	// No finalizer, because nothing was claimed.
	current := getNetwork(t, "netretain")
	if len(current.Finalizers) != 0 {
		t.Fatalf("a Retain network claimed a finalizer: %v", current.Finalizers)
	}

	if err := k8sClient.Delete(testCtx, net); err != nil {
		t.Fatalf("deleting the CR: %v", err)
	}
	eventually(t, "the CR to be gone", func() error {
		out := &platformv1alpha1.ManagedNetwork{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netretain"}, out)
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("still there: %v", err)
	})

	consistently(t, "the network surviving its description", 3*time.Second, func() error {
		if _, err := readVPC("netretain"); err != nil {
			return fmt.Errorf("the Vpc went with the CR: %w", err)
		}
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netretain-default",
		}, subnet); err != nil {
			return fmt.Errorf("the Subnet went with the CR: %w", err)
		}
		return nil
	})
}

// TestDeleteCascadesInOrder: the router goes last, and only once its subnets
// are really gone. Every subnet is finalized against that router, so removing
// it first strands them permanently — measured on this lab two hours after a
// delete, with the subnet still terminating and its addresses still missing
// from the pool.
func TestDeleteCascadesInOrder(t *testing.T) {
	net := mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netcascade"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:           "10.200.140.0/22",
			DeletionPolicy: "Delete",
		},
	})

	eventually(t, "the finalizer to be claimed", func() error {
		current := getNetwork(t, "netcascade")
		if len(current.Finalizers) == 0 {
			return fmt.Errorf("no finalizer yet")
		}
		if _, err := readVPC("netcascade"); err != nil {
			return err
		}
		return nil
	})

	if err := k8sClient.Delete(testCtx, net); err != nil {
		t.Fatalf("deleting the CR: %v", err)
	}

	eventually(t, "everything to be gone", func() error {
		for _, check := range []struct {
			what string
			gvk  schema.GroupVersionKind
			name string
		}{
			{"Vpc", vpcGVK, "netcascade"},
			{"Subnet", subnetGVK, "netcascade-default"},
			{"VpcDns", vpcDNSGVK, "netcascade-dns"},
		} {
			obj := &unstructured.Unstructured{}
			obj.SetGroupVersionKind(check.gvk)
			err := k8sClient.Get(testCtx, types.NamespacedName{Name: check.name}, obj)
			if err == nil {
				return fmt.Errorf("%s/%s is still there", check.what, check.name)
			}
			if !apierrors.IsNotFound(err) {
				return err
			}
		}
		out := &platformv1alpha1.ManagedNetwork{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netcascade"}, out); !apierrors.IsNotFound(err) {
			return fmt.Errorf("the CR is still held: %v", out.Finalizers)
		}
		return nil
	})
}

// TestDeleteWaitsRatherThanStranding: while a subnet is still finalizing, the
// router must not go. The endpoint this replaces answered 409 with the same
// list and the words "retry in a moment" — and nobody retried.
func TestDeleteWaitsRatherThanStranding(t *testing.T) {
	net := mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netstuck"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:           "10.200.144.0/22",
			DeletionPolicy: "Delete",
		},
	})

	eventually(t, "the subnet", func() error {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		return k8sClient.Get(testCtx, types.NamespacedName{Name: "netstuck-default"}, subnet)
	})

	// Stand in for kube-ovn taking its time: a finalizer nothing will remove.
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netstuck-default"}, subnet); err != nil {
		t.Fatalf("reading the subnet: %v", err)
	}
	subnet.SetFinalizers([]string{"test.kubevirt-ui.io/hold"})
	if err := k8sClient.Update(testCtx, subnet); err != nil {
		t.Fatalf("holding the subnet: %v", err)
	}

	if err := k8sClient.Delete(testCtx, net); err != nil {
		t.Fatalf("deleting the CR: %v", err)
	}

	eventually(t, "the wait to be reported", func() error {
		out := &platformv1alpha1.ManagedNetwork{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netstuck"}, out); err != nil {
			return err
		}
		cond := networkCondition(out, platformv1alpha1.ConditionDeleting)
		if cond == nil || cond.Reason != "Draining" {
			return fmt.Errorf("condition = %v", cond)
		}
		if !strings.Contains(cond.Message, "netstuck-default") {
			return fmt.Errorf("the message does not name what it waits for: %s", cond.Message)
		}
		return nil
	})

	// The router must still be there. Deleting it now is the permanent damage.
	consistently(t, "the router outliving its subnets", 4*time.Second, func() error {
		if _, err := readVPC("netstuck"); err != nil {
			return fmt.Errorf("the router was removed while a subnet was still "+
				"finalizing against it: %w", err)
		}
		return nil
	})

	// Let it go, and the cascade finishes on its own — no second request.
	held := &unstructured.Unstructured{}
	held.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netstuck-default"}, held); err != nil {
		t.Fatalf("reading the held subnet: %v", err)
	}
	held.SetFinalizers(nil)
	if err := k8sClient.Update(testCtx, held); err != nil {
		t.Fatalf("releasing the subnet: %v", err)
	}

	eventually(t, "the cascade to finish by itself", func() error {
		out := &platformv1alpha1.ManagedNetwork{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netstuck"}, out); !apierrors.IsNotFound(err) {
			return fmt.Errorf("still held: %v", out.Finalizers)
		}
		if _, err := readVPC("netstuck"); err == nil {
			return fmt.Errorf("the router is still there")
		}
		return nil
	})
}

// readLiveACLs returns an error rather than failing: it is called from inside
// eventually(), where a first miss is the normal case.
func readLiveACLs(subnet string) ([]acl.Rule, error) {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: subnet}, obj); err != nil {
		return nil, err
	}
	return readACLs(obj), nil
}

func liveACLsOf(t *testing.T, subnet string) []acl.Rule {
	t.Helper()
	rules, err := readLiveACLs(subnet)
	if err != nil {
		t.Fatalf("reading Subnet/%s: %v", subnet, err)
	}
	return rules
}

func aclOwnerOf(t *testing.T, subnet string) string {
	t.Helper()
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: subnet}, obj); err != nil {
		t.Fatalf("reading Subnet/%s: %v", subnet, err)
	}
	return obj.GetAnnotations()[aclOwnerAnnotation]
}

// TestAFreshNetworkIsAdoptedAndComposed: a subnet this controller just created
// has an empty list, the composer renders one, and the two differ — so adoption
// declines and says what it would add. That is deliberate: a handover is only
// automatic when it changes nothing.
func TestAFreshNetworkIsAdoptedAndComposed(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netacls"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.148.0/22"},
	})

	eventually(t, "adoption to decline and explain itself", func() error {
		net := getNetwork(t, "netacls")
		cond := networkCondition(net, platformv1alpha1.ConditionIsolated)
		if cond == nil || cond.Reason != "AdoptionWouldChange" {
			return fmt.Errorf("condition = %v", cond)
		}
		if !strings.Contains(cond.Message, "Nothing was written") {
			return fmt.Errorf("message = %s", cond.Message)
		}
		return nil
	})

	// And it wrote nothing, which is the claim.
	if rules := liveACLsOf(t, "netacls-default"); len(rules) != 0 {
		t.Fatalf("adoption wrote rules: %v", rules)
	}
	if owner := aclOwnerOf(t, "netacls-default"); owner != "" {
		t.Fatalf("adoption claimed the list: %q", owner)
	}
}

// TestOwnershipTransfersOnlyWhenItChangesNothing, and then the composer keeps
// the list. This is the handover: the rules are put in place by whatever owns
// them today, and only once the render matches does the annotation go on.
func TestOwnershipTransfersOnlyWhenItChangesNothing(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "nethandover"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.152.0/22"},
	})

	var wanted []acl.Rule
	eventually(t, "the composer to say what it wants", func() error {
		net := getNetwork(t, "nethandover")
		cond := networkCondition(net, platformv1alpha1.ConditionIsolated)
		if cond == nil || cond.Reason != "AdoptionWouldChange" {
			return fmt.Errorf("condition = %v", cond)
		}
		input, err := networkReconciler.aclInput(testCtx, net)
		if err != nil {
			return err
		}
		wanted, _ = acl.Render(input)
		if len(wanted) == 0 {
			return fmt.Errorf("nothing rendered")
		}
		return nil
	})

	// Somebody else — the isolation pass, today — puts them there.
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Name: "nethandover-default",
	}, subnet); err != nil {
		t.Fatalf("reading the subnet: %v", err)
	}
	if err := writeACLs(subnet, wanted); err != nil {
		t.Fatalf("building the list: %v", err)
	}
	if err := k8sClient.Update(testCtx, subnet); err != nil {
		t.Fatalf("writing the list: %v", err)
	}

	eventually(t, "ownership to transfer", func() error {
		if owner := aclOwnerOf(t, "nethandover-default"); owner != aclOwnerOperator {
			return fmt.Errorf("owner = %q", owner)
		}
		cond := networkCondition(getNetwork(t, "nethandover"),
			platformv1alpha1.ConditionIsolated)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("condition = %v", cond)
		}
		return nil
	})

	// The handover changed nothing on the object.
	if got := liveACLsOf(t, "nethandover-default"); !acl.Equal(got, wanted) {
		t.Fatalf("the list changed during the handover:\n  before %v\n  after  %v",
			wanted, got)
	}

	// And now it is kept. Somebody deletes a rule; it comes back.
	owned := &unstructured.Unstructured{}
	owned.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Name: "nethandover-default",
	}, owned); err != nil {
		t.Fatalf("reading the owned subnet: %v", err)
	}
	if err := writeACLs(owned, wanted[1:]); err != nil {
		t.Fatalf("removing a rule: %v", err)
	}
	if err := k8sClient.Update(testCtx, owned); err != nil {
		t.Fatalf("removing a rule: %v", err)
	}

	eventually(t, "the missing rule to come back", func() error {
		if got := liveACLsOf(t, "nethandover-default"); !acl.Equal(got, wanted) {
			return fmt.Errorf("still %d rules", len(got))
		}
		return nil
	})
}

// TestAListWithAForeignRuleIsNotTakenOver. Taking ownership means being able to
// reproduce all of it; a rule nobody here wrote is named, and the subnet keeps
// whatever wrote it. Silently dropping somebody's rule to take over a list is a
// worse outcome than not taking it over.
func TestAListWithAForeignRuleIsNotTakenOver(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netforeign"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.156.0/22"},
	})

	eventually(t, "the subnet", func() error {
		obj := &unstructured.Unstructured{}
		obj.SetGroupVersionKind(subnetGVK)
		return k8sClient.Get(testCtx, types.NamespacedName{Name: "netforeign-default"}, obj)
	})

	foreign := acl.Rule{
		Action: "allow-related", Direction: "to-lport",
		Match: "ip4.src == 192.0.2.0/24", Priority: 2900,
	}
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Name: "netforeign-default",
	}, subnet); err != nil {
		t.Fatalf("reading the subnet: %v", err)
	}
	if err := writeACLs(subnet, []acl.Rule{foreign}); err != nil {
		t.Fatalf("planting the rule: %v", err)
	}
	if err := k8sClient.Update(testCtx, subnet); err != nil {
		t.Fatalf("planting the rule: %v", err)
	}

	eventually(t, "the refusal to name the rule", func() error {
		cond := networkCondition(getNetwork(t, "netforeign"),
			platformv1alpha1.ConditionIsolated)
		if cond == nil || cond.Reason != "NotAdopted" {
			return fmt.Errorf("condition = %v", cond)
		}
		if !strings.Contains(cond.Message, "192.0.2.0/24") {
			return fmt.Errorf("the refusal does not name it: %s", cond.Message)
		}
		return nil
	})

	consistently(t, "the foreign rule surviving", 3*time.Second, func() error {
		got := liveACLsOf(t, "netforeign-default")
		if len(got) != 1 || got[0] != foreign {
			return fmt.Errorf("acls = %v", got)
		}
		if owner := aclOwnerOf(t, "netforeign-default"); owner != "" {
			return fmt.Errorf("it was claimed anyway: %q", owner)
		}
		return nil
	})
}

// TestANetworkTheOperatorCreatedIsClosedBeforeItIsReady.
//
// This is the property the UI cutover needs. On the old path the rules are part
// of the subnet manifest, so a network is isolated from the instant it exists.
// Anything that writes the subnet first and the rules afterwards has a window,
// and a window that survives a process dying is not a window — it is a network
// that stays open.
//
// So: the operator writes both, and does not call the network ready until the
// rules are on it.
func TestANetworkTheOperatorCreatedIsClosedBeforeItIsReady(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netclosedfirst"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:           "10.200.160.0/22",
			DeletionPolicy: "Delete",
		},
	})

	eventually(t, "the network to become ready", func() error {
		net := getNetwork(t, "netclosedfirst")
		cond := networkCondition(net, platformv1alpha1.ConditionNetworkReady)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("Ready = %v", cond)
		}
		return nil
	})

	// By the time it says ready, the rules are there and it owns them.
	rules := liveACLsOf(t, "netclosedfirst-default")
	if len(rules) == 0 {
		t.Fatal("ready with no isolation rules — the window this exists to close")
	}
	if owner := aclOwnerOf(t, "netclosedfirst-default"); owner != aclOwnerOperator {
		t.Fatalf("the list is unowned: %q", owner)
	}

	// And the floor is the aggregate, not an enumeration.
	var floor bool
	for _, rule := range rules {
		if rule.Action == "drop" && strings.Contains(rule.Match, "10.200.0.0/14") {
			floor = true
		}
	}
	if !floor {
		t.Fatalf("no aggregate floor in %v", rules)
	}
}

// TestAnAdoptedNetworkIsNotClaimedJustBecauseItsListIsEmpty. The shortcut above
// is for networks this controller created. A network merely described here —
// Retain, the adoption case — has an empty list because somebody chose not to
// isolate it, and writing rules onto it would be taking a decision that was
// already made.
func TestAnAdoptedNetworkIsNotClaimedJustBecauseItsListIsEmpty(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netdescribed"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.164.0/22"},
	})

	eventually(t, "the subnet", func() error {
		obj := &unstructured.Unstructured{}
		obj.SetGroupVersionKind(subnetGVK)
		return k8sClient.Get(testCtx, types.NamespacedName{Name: "netdescribed-default"}, obj)
	})

	consistently(t, "the empty list staying empty and unclaimed", 3*time.Second, func() error {
		if rules := liveACLsOf(t, "netdescribed-default"); len(rules) != 0 {
			return fmt.Errorf("rules were written onto a described network: %v", rules)
		}
		if owner := aclOwnerOf(t, "netdescribed-default"); owner != "" {
			return fmt.Errorf("claimed: %q", owner)
		}
		return nil
	})
}

// TestTheSubnetIsNeverBrieflyOpen: the rules must be in the create payload, not
// patched on afterwards.
//
// A subnet created open and closed a moment later is open for that moment.
// kube-ovn realises it as soon as it exists, and "eventually consistent" is not
// a property to have on the boundary between two tenants — the path this
// replaces put the rules in the manifest, and so must this one.
//
// `generation` is the evidence: the API server sets it to 1 on create and
// increments it on every spec change. Rules present at generation 1 can only
// have arrived with the object.
func TestTheSubnetIsNeverBrieflyOpen(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netatomic"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:           "10.200.168.0/22",
			DeletionPolicy: "Delete",
		},
	})

	var subnet *unstructured.Unstructured
	eventually(t, "the subnet", func() error {
		obj := &unstructured.Unstructured{}
		obj.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netatomic-default",
		}, obj); err != nil {
			return err
		}
		subnet = obj
		return nil
	})

	rules := readACLs(subnet)
	if len(rules) == 0 {
		t.Fatal("the subnet exists with no rules — it is open right now")
	}
	if got := subnet.GetGeneration(); got != 1 {
		t.Fatalf("generation %d: the spec was written more than once, so the "+
			"rules were patched on rather than shipped with the object", got)
	}
	if owner := subnet.GetAnnotations()[aclOwnerAnnotation]; owner != aclOwnerOperator {
		t.Fatalf("the list arrived unowned: %q", owner)
	}

	// And it really is closed, not merely populated.
	if got := acl.Evaluate(rules, netip.MustParseAddr("10.200.4.9"), "to-lport"); got != acl.Dropped {
		t.Errorf("another tenant reaches it at generation 1: %s", got)
	}
	if got := acl.Evaluate(rules, netip.MustParseAddr("8.8.8.8"), "to-lport"); got != acl.Allowed {
		t.Errorf("the internet is blocked: %s", got)
	}
}

// TestTheControllerStandsDownForSomebodyElsesDelete.
//
// Writing to an object with a deletionTimestamp is legal and is exactly the
// wrong thing to do: CreateOrUpdate on a subnet that has just been deleted
// either keeps a dying object alive or recreates one the deleter believes is
// gone. Two reconcilers pulling in opposite directions is how a teardown
// wedges, and the half of it this controller owns is not writing.
func TestTheControllerStandsDownForSomebodyElsesDelete(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netstanddown"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.172.0/22"},
	})

	eventually(t, "the subnet", func() error {
		obj := &unstructured.Unstructured{}
		obj.SetGroupVersionKind(subnetGVK)
		return k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netstanddown-default",
		}, obj)
	})

	// Somebody else starts taking it apart, and something holds it there — the
	// state a real teardown passes through while kube-ovn finalizes.
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Name: "netstanddown-default",
	}, subnet); err != nil {
		t.Fatalf("reading the subnet: %v", err)
	}
	subnet.SetFinalizers([]string{"test.kubevirt-ui.io/hold"})
	if err := k8sClient.Update(testCtx, subnet); err != nil {
		t.Fatalf("holding the subnet: %v", err)
	}
	if err := k8sClient.Delete(testCtx, subnet); err != nil {
		t.Fatalf("deleting the subnet: %v", err)
	}

	eventually(t, "the controller to say it has stopped", func() error {
		cond := networkCondition(getNetwork(t, "netstanddown"),
			platformv1alpha1.ConditionNetworkReady)
		if cond == nil || cond.Reason != "BeingDeleted" {
			return fmt.Errorf("condition = %v", cond)
		}
		return nil
	})

	// It is dying, and it stays dying: nothing here revived it or held it open.
	before := getNetwork(t, "netstanddown")
	poke(t, "netstanddown", "1")
	consistently(t, "the subnet still on its way out", 3*time.Second, func() error {
		live := &unstructured.Unstructured{}
		live.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netstanddown-default",
		}, live); err != nil {
			return err
		}
		if live.GetDeletionTimestamp().IsZero() {
			return fmt.Errorf("the deletion was undone")
		}
		if live.GetUID() != subnet.GetUID() {
			return fmt.Errorf("it was recreated: %s != %s", live.GetUID(), subnet.GetUID())
		}
		return nil
	})
	_ = before

	// Let it go.
	held := &unstructured.Unstructured{}
	held.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Name: "netstanddown-default",
	}, held); err != nil {
		t.Fatalf("reading the held subnet: %v", err)
	}
	held.SetFinalizers(nil)
	if err := k8sClient.Update(testCtx, held); err != nil {
		t.Fatalf("releasing the subnet: %v", err)
	}
}

// TestADeclaredPeeringOpensThePrefixBeforeAnythingIsRouted.
//
// The composer reads the declaration, not the routers. That is what breaks the
// circle: the peering controller waits for the allow and the allow would
// otherwise be waiting for the peering entry, so one of the two has to look at
// the object instead of at the wire.
func TestADeclaredPeeringOpensThePrefixBeforeAnythingIsRouted(t *testing.T) {
	for _, spec := range []struct{ name, cidr string }{
		{"netpeer-a", "10.200.176.0/22"},
		{"netpeer-b", "10.200.180.0/22"},
	} {
		mustNetwork(t, &platformv1alpha1.ManagedNetwork{
			ObjectMeta: metav1.ObjectMeta{Name: spec.name},
			Spec: platformv1alpha1.ManagedNetworkSpec{
				CIDR: spec.cidr, DeletionPolicy: "Delete",
			},
		})
	}

	eventually(t, "both networks closed to each other", func() error {
		rules, err := readLiveACLs("netpeer-a-default")
		if err != nil {
			return err
		}
		if len(rules) == 0 {
			return fmt.Errorf("no rules yet")
		}
		if got := acl.Evaluate(rules,
			netip.MustParseAddr("10.200.180.9"), "to-lport"); got != acl.Dropped {
			return fmt.Errorf("already open: %s", got)
		}
		return nil
	})

	link := &platformv1alpha1.ManagedNetworkPeering{
		ObjectMeta: metav1.ObjectMeta{Name: "netpeer-a-netpeer-b"},
		Spec: platformv1alpha1.ManagedNetworkPeeringSpec{
			Networks: []string{"netpeer-a", "netpeer-b"},
		},
	}
	if err := k8sClient.Create(testCtx, link); err != nil {
		t.Fatalf("declaring the peering: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, link) })

	eventually(t, "the prefix to open on both sides from the declaration alone", func() error {
		for _, pair := range [][2]string{
			{"netpeer-a-default", "10.200.180.9"},
			{"netpeer-b-default", "10.200.176.9"},
		} {
			rules, err := readLiveACLs(pair[0])
			if err != nil {
				return err
			}
			if got := acl.Evaluate(rules,
				netip.MustParseAddr(pair[1]), "to-lport"); got != acl.Allowed {
				return fmt.Errorf("%s still drops %s: %s", pair[0], pair[1], got)
			}
		}
		return nil
	})
}

// TestAnUnacceptedDeclarationOpensNothing.
//
// The composer opens the prefix from the declaration, and a declaration is
// something anybody who can create an object can write. If it trusted the spec,
// naming two networks in a CR would open an allow between them even when the
// peering is refused and no route is ever laid — a hole in the isolation with
// nothing going through it, which is the worst of both.
func TestAnUnacceptedDeclarationOpensNothing(t *testing.T) {
	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netguard"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR: "10.200.184.0/22", DeletionPolicy: "Delete",
		},
	})

	// A real network with a real subnet, whose rule list belongs to something
	// else — so the peering will be refused, and the allow must not appear on
	// the composed side either.
	mustPeeredNetwork(t, "netstranger", "10.200.188.0/22")
	setACLs(t, "netstranger-default", false, []map[string]any{
		dropFrom("10.200.184.0/22")})

	eventually(t, "the network closed to the stranger", func() error {
		rules, err := readLiveACLs("netguard-default")
		if err != nil {
			return err
		}
		if got := acl.Evaluate(rules,
			netip.MustParseAddr("10.200.188.9"), "to-lport"); got != acl.Dropped {
			return fmt.Errorf("already open: %s", got)
		}
		return nil
	})

	link := &platformv1alpha1.ManagedNetworkPeering{
		ObjectMeta: metav1.ObjectMeta{Name: "netguard-stranger"},
		Spec: platformv1alpha1.ManagedNetworkPeeringSpec{
			Networks: []string{"netguard", "netstranger"},
		},
	}
	if err := k8sClient.Create(testCtx, link); err != nil {
		t.Fatalf("declaring: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, link) })

	eventually(t, "the refusal", func() error {
		out := &platformv1alpha1.ManagedNetworkPeering{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netguard-stranger",
		}, out); err != nil {
			return err
		}
		cond := apimeta.FindStatusCondition(out.Status.Conditions,
			platformv1alpha1.ConditionPeeringAccepted)
		if cond == nil || cond.Status != metav1.ConditionFalse {
			return fmt.Errorf("Accepted = %v", cond)
		}
		if cond.Reason != "IsolationNotOurs" {
			return fmt.Errorf("reason = %s", cond.Reason)
		}
		return nil
	})

	consistently(t, "the composed side staying shut", 6*time.Second, func() error {
		rules, err := readLiveACLs("netguard-default")
		if err != nil {
			return err
		}
		if got := acl.Evaluate(rules,
			netip.MustParseAddr("10.200.188.9"), "to-lport"); got != acl.Dropped {
			return fmt.Errorf("an unaccepted declaration opened the prefix: %s", got)
		}
		return nil
	})
}

// TestTheAllowOutlivesTheRoutes is the deletion ordering.
//
// The obvious reading of "this peering is being deleted, stop allowing it"
// takes the allow off the moment the object is marked, while the finalizer is
// still pulling the routes off the routers — for as long as that takes, the
// traffic is routed at a prefix that now drops it. The same black hole,
// arrived at from the other end.
func TestTheAllowOutlivesTheRoutes(t *testing.T) {
	for _, spec := range []struct{ name, cidr string }{
		{"netlast-a", "10.200.192.0/22"},
		{"netlast-b", "10.200.196.0/22"},
	} {
		mustNetwork(t, &platformv1alpha1.ManagedNetwork{
			ObjectMeta: metav1.ObjectMeta{Name: spec.name},
			Spec: platformv1alpha1.ManagedNetworkSpec{
				CIDR: spec.cidr, DeletionPolicy: "Delete",
			},
		})
	}

	link := &platformv1alpha1.ManagedNetworkPeering{
		ObjectMeta: metav1.ObjectMeta{
			Name: "netlast-a-netlast-b",
			// Ours, so the object stays while its own teardown is watched.
			Finalizers: []string{"test.kubevirt-ui.io/hold"},
		},
		Spec: platformv1alpha1.ManagedNetworkPeeringSpec{
			Networks: []string{"netlast-a", "netlast-b"},
		},
	}
	if err := k8sClient.Create(testCtx, link); err != nil {
		t.Fatalf("declaring: %v", err)
	}
	t.Cleanup(func() {
		current := &platformv1alpha1.ManagedNetworkPeering{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netlast-a-netlast-b",
		}, current); err == nil {
			current.Finalizers = nil
			_ = k8sClient.Update(testCtx, current)
		}
	})

	allowed := func() (bool, error) {
		rules, err := readLiveACLs("netlast-a-default")
		if err != nil {
			return false, err
		}
		return acl.Evaluate(rules,
			netip.MustParseAddr("10.200.196.9"), "to-lport") == acl.Allowed, nil
	}

	eventually(t, "the prefix to open", func() error {
		open, err := allowed()
		if err != nil {
			return err
		}
		if !open {
			return fmt.Errorf("still shut")
		}
		return nil
	})

	if err := k8sClient.Delete(testCtx, link); err != nil {
		t.Fatalf("deleting: %v", err)
	}

	// The cached read lags the delete by a beat; the interesting window starts
	// once the mark is visible.
	eventually(t, "the deletion to be visible", func() error {
		out := &platformv1alpha1.ManagedNetworkPeering{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netlast-a-netlast-b",
		}, out); err != nil {
			return err
		}
		if out.DeletionTimestamp.IsZero() {
			return fmt.Errorf("not marked yet")
		}
		return nil
	})

	// Marked for deletion and held. The routes may or may not be off yet; what
	// must not happen is the allow going first.
	consistently(t, "the allow outliving the marked object", 5*time.Second, func() error {
		out := &platformv1alpha1.ManagedNetworkPeering{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netlast-a-netlast-b",
		}, out); err != nil {
			return fmt.Errorf("the object went while it was still held: %w", err)
		}
		open, err := allowed()
		if err != nil {
			return err
		}
		if !open {
			return fmt.Errorf("the allow came off while the object was still " +
				"being torn down — the routes would be pointing at a drop")
		}
		return nil
	})

	// Let it finish, and the allow follows.
	current := &platformv1alpha1.ManagedNetworkPeering{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Name: "netlast-a-netlast-b",
	}, current); err != nil {
		t.Fatalf("reading the held object: %v", err)
	}
	current.Finalizers = nil
	if err := k8sClient.Update(testCtx, current); err != nil {
		t.Fatalf("releasing: %v", err)
	}

	eventually(t, "the allow to follow the object", func() error {
		open, err := allowed()
		if err != nil {
			return err
		}
		if open {
			return fmt.Errorf("still open after the peering is gone")
		}
		return nil
	})
}

// TestALegIsTakenBackWhenItStopsBeingDeclared.
//
// The missing half, and it cost a live A/B: setting `attachments` back to one
// entry did nothing to the VPC, because the renderer merges and never removes.
// Detaching had to be done by editing the Vpc by hand, which is not a way to
// run a fabric — and it is the same "on but not off" shape as every other
// switch in this migration.
func TestALegIsTakenBackWhenItStopsBeingDeclared(t *testing.T) {
	mustExternalSubnet(t, "wd-transit", "10.199.20.0/22", "10.199.20.254")
	mustExternalSubnet(t, "wd-external", "10.199.21.0/22", "10.199.21.254")

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netwd"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR: "10.200.72.0/22", Folder: "poc", Environment: "dev",
			ExternalPlane: &platformv1alpha1.ExternalPlane{
				Attachments:  []string{"wd-transit", "wd-external"},
				EgressSubnet: "wd-external",
			},
		},
	})

	eventually(t, "both legs", func() error {
		vpc, err := liveVPCObject("netwd")
		if err != nil {
			return err
		}
		attached, _, _ := unstructured.NestedStringSlice(vpc.Object, "spec", "extraExternalSubnets")
		if len(attached) != 2 {
			return fmt.Errorf("attachments = %v", attached)
		}
		return nil
	})
	eventually(t, "the record of what was applied", func() error {
		if len(getNetwork(t, "netwd").Status.Attachments) != 2 {
			return fmt.Errorf("status has not caught up")
		}
		return nil
	})

	// Somebody else's leg, on the same object.
	vpc, err := liveVPCObject("netwd")
	if err != nil {
		t.Fatalf("reading the vpc: %v", err)
	}
	_ = unstructured.SetNestedStringSlice(vpc.Object,
		[]string{"wd-transit", "wd-external", "somebody-elses"},
		"spec", "extraExternalSubnets")
	if err := k8sClient.Update(testCtx, vpc); err != nil {
		t.Fatalf("planting a foreign leg: %v", err)
	}

	declare(t, "netwd", []string{"wd-transit"})

	eventually(t, "the leg to be taken back", func() error {
		vpc, err := liveVPCObject("netwd")
		if err != nil {
			return err
		}
		attached, _, _ := unstructured.NestedStringSlice(vpc.Object, "spec", "extraExternalSubnets")
		for _, name := range attached {
			if name == "wd-external" {
				return fmt.Errorf("the leg it added is still there: %v", attached)
			}
		}
		// And what it never claimed is untouched.
		for _, name := range attached {
			if name == "somebody-elses" {
				return nil
			}
		}
		return fmt.Errorf("it took somebody else's leg with it: %v", attached)
	})
}

// TestTheControlPlaneLegIsNotTakenFromUnderATenant.
//
// The tenant controller attaches it, because a tenant's workers reach their
// control plane over it. If this controller could withdraw it on a declaration
// change, one leg would have two writers with opposite intentions and the flap
// would land on the path that must not flap. An egress leg can be taken away —
// that is a deliberate loss of internet, which is the point of the two planes
// being separate — but this one cannot.
func TestTheControlPlaneLegIsNotTakenFromUnderATenant(t *testing.T) {
	mustExternalSubnet(t, "hold-transit", "10.199.22.0/22", "10.199.22.254")
	mustExternalSubnet(t, "hold-external", "10.199.23.0/22", "10.199.23.254")
	networkReconciler.TransitSubnet = "hold-transit"
	t.Cleanup(func() { networkReconciler.TransitSubnet = "" })

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "nethold"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR: "10.200.76.0/22", Folder: "poc", Environment: "dev",
			ExternalPlane: &platformv1alpha1.ExternalPlane{
				Attachments:  []string{"hold-transit", "hold-external"},
				EgressSubnet: "hold-external",
			},
		},
	})
	eventually(t, "the record of what was applied", func() error {
		if len(getNetwork(t, "nethold").Status.Attachments) != 2 {
			return fmt.Errorf("status has not caught up")
		}
		return nil
	})

	tenant := plainTenant("tenhold")
	tenant.Spec.Network = "nethold"
	mustTenant(t, tenant)

	// Both legs dropped from the declaration.
	declare(t, "nethold", nil)

	eventually(t, "the egress leg to go", func() error {
		vpc, err := liveVPCObject("nethold")
		if err != nil {
			return err
		}
		attached, _, _ := unstructured.NestedStringSlice(vpc.Object, "spec", "extraExternalSubnets")
		for _, name := range attached {
			if name == "hold-external" {
				return fmt.Errorf("still attached: %v", attached)
			}
		}
		return nil
	})

	consistently(t, "the control-plane leg to stay", 5*time.Second, func() error {
		vpc, err := liveVPCObject("nethold")
		if err != nil {
			return err
		}
		attached, _, _ := unstructured.NestedStringSlice(vpc.Object, "spec", "extraExternalSubnets")
		for _, name := range attached {
			if name == "hold-transit" {
				return nil
			}
		}
		return fmt.Errorf("it took the control-plane leg from under a tenant: %v", attached)
	})
}

// declare rewrites a network's attachments, retrying the conflict the
// controller's own status writes cause.
func declare(t *testing.T, name string, attachments []string) {
	t.Helper()
	eventually(t, "the declaration to be accepted", func() error {
		live := &platformv1alpha1.ManagedNetwork{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, live); err != nil {
			return err
		}
		live.Spec.ExternalPlane.Attachments = attachments
		// An egress subnet that is no longer attached is a declaration that
		// contradicts itself, and the controller refuses it — rightly.
		attached := false
		for _, name := range attachments {
			if name == live.Spec.ExternalPlane.EgressSubnet {
				attached = true
			}
		}
		if !attached {
			live.Spec.ExternalPlane.EgressSubnet = ""
		}
		return k8sClient.Update(testCtx, live)
	})
}
