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
	"sort"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// mustPeeredNetwork is a bare VPC with one subnet — the least a peering needs.
func mustPeeredNetwork(t *testing.T, name, cidr string) {
	t.Helper()
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	vpc.SetName(name)
	_ = unstructured.SetNestedMap(vpc.Object, map[string]any{}, "spec")
	if err := k8sClient.Create(testCtx, vpc); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating Vpc/%s: %v", name, err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, vpc) })

	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	subnet.SetName(name + "-default")
	_ = unstructured.SetNestedMap(subnet.Object, map[string]any{
		"protocol": "IPv4", "cidrBlock": cidr, "vpc": name,
	}, "spec")
	if err := k8sClient.Create(testCtx, subnet); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating Subnet/%s: %v", name, err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, subnet) })
}

func mustPeering(t *testing.T, name string, networks ...string) *platformv1alpha1.ManagedNetworkPeering {
	t.Helper()
	link := &platformv1alpha1.ManagedNetworkPeering{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec:       platformv1alpha1.ManagedNetworkPeeringSpec{Networks: networks},
	}
	if err := k8sClient.Create(testCtx, link); err != nil {
		t.Fatalf("creating peering %s: %v", name, err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, link) })
	return link
}

func getPeering(t *testing.T, name string) *platformv1alpha1.ManagedNetworkPeering {
	t.Helper()
	out := &platformv1alpha1.ManagedNetworkPeering{}
	var last error
	for i := 0; i < 50; i++ {
		last = k8sClient.Get(testCtx, types.NamespacedName{Name: name}, out)
		if last == nil {
			return out
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("reading peering %s: %v", name, last)
	return nil
}

// peeringEntriesOf is the remote names in a VPC's peering list.
func peeringEntriesOf(t *testing.T, vpc string) []string {
	t.Helper()
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(vpcGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: vpc}, obj); err != nil {
		t.Fatalf("reading Vpc/%s: %v", vpc, err)
	}
	entries, _, _ := unstructured.NestedSlice(obj.Object, "spec", "vpcPeerings")
	var out []string
	for _, raw := range entries {
		entry, _ := raw.(map[string]any)
		if name, _ := entry["remoteVpc"].(string); name != "" {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

// TestBothEndsOrNeither is the base case, and it checks the part that is easy
// to leave out: the policy route above the egress gateway's catch-all, without
// which the traffic hairpins and the peering looks broken for no visible
// reason.
func TestBothEndsOrNeither(t *testing.T) {
	mustPeeredNetwork(t, "pa", "10.210.0.0/22")
	mustPeeredNetwork(t, "pb", "10.210.4.0/22")
	mustPeering(t, "pa-pb", "pa", "pb")

	eventually(t, "the peering to be established", func() error {
		link := getPeering(t, "pa-pb")
		cond := apimeta.FindStatusCondition(link.Status.Conditions,
			platformv1alpha1.ConditionEstablished)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("condition = %v", cond)
		}
		if link.Status.LinkCIDR == "" {
			return fmt.Errorf("no link chosen")
		}
		return nil
	})

	for _, pair := range [][2]string{{"pa", "pb"}, {"pb", "pa"}} {
		local, remote := pair[0], pair[1]
		if got := peeringEntriesOf(t, local); len(got) != 1 || got[0] != remote {
			t.Fatalf("%s peers with %v", local, got)
		}

		obj := &unstructured.Unstructured{}
		obj.SetGroupVersionKind(vpcGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: local}, obj); err != nil {
			t.Fatalf("reading Vpc/%s: %v", local, err)
		}
		routes, _, _ := unstructured.NestedSlice(obj.Object, "spec", "staticRoutes")
		policies, _, _ := unstructured.NestedSlice(obj.Object, "spec", "policyRoutes")
		if len(routes) != 1 {
			t.Errorf("%s has %d routes", local, len(routes))
		}
		if len(policies) != 1 {
			t.Fatalf("%s has no policy route: the traffic will hairpin out to the "+
				"upstream router and the peering will look broken for no reason", local)
		}
		policy, _ := policies[0].(map[string]any)
		if policy["priority"] != int64(31001) {
			t.Errorf("%s policy at %v — below the gateway catch-all it does nothing",
				local, policy["priority"])
		}
	}
}

// TestTwoPeeringsGetDifferentLinks. Two peerings touching the same VPC at once
// each used to compute the list from the same read, and the second dropped the
// first entry: both calls succeeded and half the links quietly did not exist.
func TestTwoPeeringsGetDifferentLinks(t *testing.T) {
	for i, cidr := range []string{"10.211.0.0/22", "10.211.4.0/22", "10.211.8.0/22"} {
		mustPeeredNetwork(t, fmt.Sprintf("pc%d", i), cidr)
	}
	mustPeering(t, "pc0-pc1", "pc0", "pc1")
	mustPeering(t, "pc0-pc2", "pc0", "pc2")

	eventually(t, "both peerings established on distinct links", func() error {
		first := getPeering(t, "pc0-pc1")
		second := getPeering(t, "pc0-pc2")
		for _, link := range []*platformv1alpha1.ManagedNetworkPeering{first, second} {
			cond := apimeta.FindStatusCondition(link.Status.Conditions,
				platformv1alpha1.ConditionEstablished)
			if cond == nil || cond.Status != metav1.ConditionTrue {
				return fmt.Errorf("%s: %v", link.Name, cond)
			}
		}
		if first.Status.LinkCIDR == second.Status.LinkCIDR {
			return fmt.Errorf("both took %s — two routers holding the same "+
				"address on a point-to-point link", first.Status.LinkCIDR)
		}
		return nil
	})

	// And neither dropped the other's entry from the VPC they share.
	if got := peeringEntriesOf(t, "pc0"); len(got) != 2 {
		t.Fatalf("pc0 peers with %v — one of the two entries was overwritten", got)
	}
}

// TestAOneSidedPeeringIsNeverWritten.
//
// A peering on one side only is worse than none: the routes point into a link
// the other router does not hold, so the traffic goes there and dies, where
// before it would at least have taken the default route.
//
// The rollback exists for the race where a router disappears mid-write. Relying
// on it as the normal path is worse than the disease: the first end is written
// and removed again on every retry, so the black hole flaps every few seconds
// instead of staying. Both routers are checked before either is touched.
func TestAOneSidedPeeringIsNeverWritten(t *testing.T) {
	mustPeeredNetwork(t, "pd", "10.212.0.0/22")
	// The far side has a subnet but no router: the second write cannot land.
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	subnet.SetName("pe-default")
	_ = unstructured.SetNestedMap(subnet.Object, map[string]any{
		"protocol": "IPv4", "cidrBlock": "10.212.4.0/22", "vpc": "pe",
	}, "spec")
	if err := k8sClient.Create(testCtx, subnet); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the orphan subnet: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, subnet) })

	mustPeering(t, "pd-pe", "pd", "pe")

	eventually(t, "the refusal", func() error {
		link := getPeering(t, "pd-pe")
		cond := apimeta.FindStatusCondition(link.Status.Conditions,
			platformv1alpha1.ConditionEstablished)
		if cond == nil || cond.Status != metav1.ConditionFalse {
			return fmt.Errorf("condition = %v", cond)
		}
		if cond.Reason != "NoSuchNetwork" {
			return fmt.Errorf("reason = %s", cond.Reason)
		}
		if !strings.Contains(cond.Message, "nothing was written") {
			return fmt.Errorf("message = %s", cond.Message)
		}
		return nil
	})

	// Nothing was written, and it stays that way. Watched for longer than the
	// retry interval: writing the first end and taking it off again on every
	// pass would be a black hole that flaps rather than one that stays, and a
	// short window would miss it.
	consistently(t, "no half-peering, ever", 8*time.Second, func() error {
		if got := peeringEntriesOf(t, "pd"); len(got) != 0 {
			return fmt.Errorf("pd peers with %v", got)
		}
		return nil
	})
}

// TestTheUndoSurvivesTheProcess is the reason the record is in status.
//
// The endpoint this replaces held the applied ends in a local variable and undid
// them in an exception handler. That covers the failure it was written for and
// not the one that matters: a process stopping between the two writes leaves a
// black hole, and nothing anywhere remembers to undo it. Here the status is the
// record, so a controller that has never seen this peering before can still
// finish the job.
func TestTheUndoSurvivesTheProcess(t *testing.T) {
	mustPeeredNetwork(t, "pf", "10.213.0.0/22")
	mustPeeredNetwork(t, "pg", "10.213.4.0/22")

	// The state a crash leaves behind: one end written, the object saying so,
	// and nothing in memory anywhere.
	link := &platformv1alpha1.ManagedNetworkPeering{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "pf-pg",
			Finalizers: []string{peeringFinalizer},
			// Paused, so the state can be planted before the controller looks.
			Annotations: map[string]string{pausedAnnotation: "true"},
		},
		Spec: platformv1alpha1.ManagedNetworkPeeringSpec{
			Networks: []string{"pf", "pg"},
			LinkCIDR: "169.254.101.200/30",
		},
	}
	if err := k8sClient.Create(testCtx, link); err != nil {
		t.Fatalf("creating the peering: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, link) })

	planted := getPeering(t, "pf-pg")
	planted.Status.LinkCIDR = "169.254.101.200/30"
	planted.Status.Legs = []platformv1alpha1.PeeringLeg{
		{Network: "pf", ConnectIP: "169.254.101.201", Applied: true},
		{Network: "pg", ConnectIP: "169.254.101.202"},
	}
	if err := k8sClient.Status().Update(testCtx, planted); err != nil {
		t.Fatalf("planting the status: %v", err)
	}

	// And the half-peering itself, on the router.
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "pf"}, vpc); err != nil {
		t.Fatalf("reading Vpc/pf: %v", err)
	}
	_ = unstructured.SetNestedSlice(vpc.Object, []any{map[string]any{
		"remoteVpc": "pg", "localConnectIP": "169.254.101.201/30",
	}}, "spec", "vpcPeerings")
	if err := k8sClient.Update(testCtx, vpc); err != nil {
		t.Fatalf("planting the half-peering: %v", err)
	}

	// A controller that has never seen this object starts looking at it.
	current := getPeering(t, "pf-pg")
	unpaused := current.DeepCopy()
	delete(unpaused.Annotations, pausedAnnotation)
	if err := k8sClient.Update(testCtx, unpaused); err != nil {
		t.Fatalf("unpausing: %v", err)
	}

	eventually(t, "the peering to be completed rather than abandoned", func() error {
		if got := peeringEntriesOf(t, "pg"); len(got) != 1 || got[0] != "pf" {
			return fmt.Errorf("pg peers with %v", got)
		}
		if got := peeringEntriesOf(t, "pf"); len(got) != 1 || got[0] != "pg" {
			return fmt.Errorf("pf peers with %v", got)
		}
		return nil
	})
}

// TestRemovingThePeeringRemovesBothEnds, including the routes and the policy —
// leaving a route to a prefix nothing carries is the same black hole from the
// other direction.
func TestRemovingThePeeringRemovesBothEnds(t *testing.T) {
	mustPeeredNetwork(t, "ph", "10.214.0.0/22")
	mustPeeredNetwork(t, "pi", "10.214.4.0/22")
	link := mustPeering(t, "ph-pi", "ph", "pi")

	eventually(t, "both ends", func() error {
		for _, vpc := range []string{"ph", "pi"} {
			if len(peeringEntriesOf(t, vpc)) != 1 {
				return fmt.Errorf("%s not peered yet", vpc)
			}
		}
		return nil
	})

	if err := k8sClient.Delete(testCtx, link); err != nil {
		t.Fatalf("deleting the peering: %v", err)
	}

	eventually(t, "both ends to be gone", func() error {
		for _, vpc := range []string{"ph", "pi"} {
			obj := &unstructured.Unstructured{}
			obj.SetGroupVersionKind(vpcGVK)
			if err := k8sClient.Get(testCtx, types.NamespacedName{Name: vpc}, obj); err != nil {
				return err
			}
			entries, _, _ := unstructured.NestedSlice(obj.Object, "spec", "vpcPeerings")
			routes, _, _ := unstructured.NestedSlice(obj.Object, "spec", "staticRoutes")
			policies, _, _ := unstructured.NestedSlice(obj.Object, "spec", "policyRoutes")
			if len(entries) != 0 || len(routes) != 0 || len(policies) != 0 {
				return fmt.Errorf("%s still has %d peerings, %d routes, %d policies",
					vpc, len(entries), len(routes), len(policies))
			}
		}
		out := &platformv1alpha1.ManagedNetworkPeering{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "ph-pi"}, out); !apierrors.IsNotFound(err) {
			return fmt.Errorf("the object is still held: %v", out.Finalizers)
		}
		return nil
	})
}
