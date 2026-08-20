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
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
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
