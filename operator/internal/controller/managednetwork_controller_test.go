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
	current := getNetwork(t, "netacl")
	patched := current.DeepCopy()
	patched.Annotations = map[string]string{"test.kubevirt-ui.io/poke": "1"}
	if err := k8sClient.Update(testCtx, patched); err != nil {
		t.Fatalf("poking: %v", err)
	}

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

	current := getNetwork(t, "netkeep")
	patched := current.DeepCopy()
	patched.Annotations = map[string]string{"test.kubevirt-ui.io/poke": "1"}
	if err := k8sClient.Update(testCtx, patched); err != nil {
		t.Fatalf("poking: %v", err)
	}

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
		current := getNetwork(t, "netquiet")
		patched := current.DeepCopy()
		if patched.Annotations == nil {
			patched.Annotations = map[string]string{}
		}
		patched.Annotations["test.kubevirt-ui.io/poke"] = fmt.Sprintf("%d", i)
		if err := k8sClient.Patch(testCtx, patched, client.MergeFrom(current)); err != nil {
			t.Fatalf("poking: %v", err)
		}
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

	eventually(t, "DNSReady", func() error {
		net := getNetwork(t, "netdns")
		cond := networkCondition(net, platformv1alpha1.ConditionDNSReady)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("condition = %v", cond)
		}
		if net.Status.ServiceRoute != "10.96.0.0/12 via 10.16.0.1" {
			return fmt.Errorf("status.serviceRoute = %q", net.Status.ServiceRoute)
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

func liveACLsOf(t *testing.T, subnet string) []acl.Rule {
	t.Helper()
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(subnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: subnet}, obj); err != nil {
		t.Fatalf("reading Subnet/%s: %v", subnet, err)
	}
	return readACLs(obj)
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
