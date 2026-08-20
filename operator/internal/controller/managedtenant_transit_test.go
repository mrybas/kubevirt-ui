package controller

import (
	"fmt"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	"github.com/mrybas/kubevirt-ui/operator/internal/transit"
)

// mustTransitSubnet is the plane itself: the underlay leg with no gateway on
// it, which is the whole reason a control plane stays reachable when one falls
// over.
func mustTransitSubnet(t *testing.T, name string) {
	t.Helper()
	mustExcludingSubnet(t, name, "10.199.0.0/22", []string{"10.199.0.1..10.199.0.255"})
}

func mustPlainVPC(t *testing.T, name string) {
	t.Helper()
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	vpc.SetName(name)
	if err := k8sClient.Create(testCtx, vpc); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating vpc %s: %v", name, err)
	}
}

// mustEIP plays kube-ovn's allocator: the object, then the address on it.
func mustEIP(t *testing.T, name, subnet, address string) {
	t.Helper()
	eip := &unstructured.Unstructured{}
	eip.SetGroupVersionKind(ovnEipGVK)
	eip.SetName(name)
	_ = unstructured.SetNestedMap(eip.Object, map[string]any{
		"externalSubnet": subnet, "type": "nat",
	}, "spec")
	if err := k8sClient.Create(testCtx, eip); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating eip %s: %v", name, err)
	}
	if address == "" {
		return
	}
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(ovnEipGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, live); err != nil {
		t.Fatalf("reading eip %s: %v", name, err)
	}
	_ = unstructured.SetNestedField(live.Object, address, "status", "v4Ip")
	if err := k8sClient.Status().Update(testCtx, live); err != nil {
		t.Fatalf("assigning %s: %v", address, err)
	}
}

func mustSNATRule(t *testing.T, name, vpc, subnet, eip string) {
	t.Helper()
	rule := &unstructured.Unstructured{}
	rule.SetGroupVersionKind(ovnSnatGVK)
	rule.SetName(name)
	_ = unstructured.SetNestedMap(rule.Object, map[string]any{
		"ovnEip": eip, "vpc": vpc, "vpcSubnet": subnet,
	}, "spec")
	if err := k8sClient.Create(testCtx, rule); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating snat %s: %v", name, err)
	}
}

func transitVPC(t *testing.T, name string) *unstructured.Unstructured {
	t.Helper()
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{Name: name}, vpc); err != nil {
		t.Fatalf("reading vpc %s: %v", name, err)
	}
	return vpc
}

func transitReconciler(subnet string) *ManagedTenantReconciler {
	return &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
		TransitSubnet: subnet,
	}
}

// TestTheLegAndItsGuardArriveTogether.
//
// Policy routes are evaluated before static ones, so an egress gateway's
// catch-all swallows the packets going one hop to the control plane. A VPC
// attached without the guard has a leg it cannot use for the one thing the leg
// is for, so both are written at once.
func TestTheLegAndItsGuardArriveTogether(t *testing.T) {
	mustTransitSubnet(t, "transit-a")
	mustPlainVPC(t, "net-tra")
	reconciler := transitReconciler("transit-a")

	obj := vpcTalosTenant("tra")
	obj.Spec.Network = "net-tra"
	if err := reconciler.attachToTransit(testCtx, "net-tra", "transit-a", "10.199.0.0/22"); err != nil {
		t.Fatalf("attach: %v", err)
	}

	vpc := transitVPC(t, "net-tra")
	external, _, _ := unstructured.NestedBool(vpc.Object, "spec", "enableExternal")
	subnets, _, _ := unstructured.NestedStringSlice(vpc.Object, "spec", "extraExternalSubnets")
	policies, _, _ := unstructured.NestedSlice(vpc.Object, "spec", "policyRoutes")
	if !external || len(subnets) != 1 || subnets[0] != "transit-a" {
		t.Errorf("enableExternal=%v subnets=%v", external, subnets)
	}
	if len(policies) != 1 {
		t.Fatalf("policyRoutes = %v — the leg is there and the guard is not", policies)
	}
	guard, _ := policies[0].(map[string]any)
	if guard["match"] != "ip4.dst == 10.199.0.0/22" {
		t.Errorf("guard = %v", guard)
	}
	if priority, _ := guard["priority"].(int64); priority != transit.GuardPriority {
		t.Errorf("guard priority = %v, which does not beat a gateway's catch-all", guard["priority"])
	}

	// Twice is once: neither the attachment nor the guard is duplicated.
	if err := reconciler.attachToTransit(testCtx, "net-tra", "transit-a", "10.199.0.0/22"); err != nil {
		t.Fatalf("second attach: %v", err)
	}
	vpc = transitVPC(t, "net-tra")
	subnets, _, _ = unstructured.NestedStringSlice(vpc.Object, "spec", "extraExternalSubnets")
	policies, _, _ = unstructured.NestedSlice(vpc.Object, "spec", "policyRoutes")
	if len(subnets) != 1 || len(policies) != 1 {
		t.Errorf("second pass duplicated: subnets=%v policies=%v", subnets, policies)
	}
}

// TestAForeignRuleHoldingTheSlotIsReportedNotAbsorbed.
//
// OVN keeps one SNAT per logical IP. Inheriting a rule whose address is on
// another network writes the guard for that address: it looks configured and
// works for nothing — internet fine, control plane unreachable.
func TestAForeignRuleHoldingTheSlotIsReportedNotAbsorbed(t *testing.T) {
	mustTransitSubnet(t, "transit-b")
	mustPlainVPC(t, "net-trb")
	mustEIP(t, "foreign-eip", "external", "10.199.4.77")
	mustSNATRule(t, "foreign-snat", "net-trb", "net-trb-default", "foreign-eip")

	obj := vpcTalosTenant("trb")
	obj.Spec.Network = "net-trb"
	address, conflict, err := transitReconciler("transit-b").ensureTenantSNAT(
		testCtx, obj, "transit-b")
	if err != nil {
		t.Fatalf("ensureTenantSNAT: %v", err)
	}
	if address != "" {
		t.Errorf("it took an address anyway: %q", address)
	}
	for _, want := range []string{"foreign-snat", "10.199.4.77", "external"} {
		if !strings.Contains(conflict, want) {
			t.Errorf("the conflict does not name %s: %s", want, conflict)
		}
	}
	// And it took nothing away: a sole claimant is reported, never removed.
	rule := &unstructured.Unstructured{}
	rule.SetGroupVersionKind(ovnSnatGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "foreign-snat"}, rule); err != nil {
		t.Errorf("the foreign rule was deleted: %v", err)
	}
}

// TestTheTransitRuleWinsAndTheOthersGo.
//
// Two rules for one logical IP cannot both be in force, and both report
// ready — the lab carried exactly that for days. The transit one is what the
// control-plane path needs, so it wins and the rest are removed rather than
// left to lie about a NAT the router does not have.
func TestTheTransitRuleWinsAndTheOthersGo(t *testing.T) {
	mustTransitSubnet(t, "transit-c")
	mustPlainVPC(t, "net-trc")
	mustEIP(t, "aaa-external-eip", "external", "10.199.4.78")
	mustSNATRule(t, "aaa-external-snat", "net-trc", "net-trc-default", "aaa-external-eip")
	mustEIP(t, "zzz-transit-eip", "transit-c", "10.199.1.30")
	mustSNATRule(t, "zzz-transit-snat", "net-trc", "net-trc-default", "zzz-transit-eip")

	obj := vpcTalosTenant("trc")
	obj.Spec.Network = "net-trc"
	address, conflict, err := transitReconciler("transit-c").ensureTenantSNAT(
		testCtx, obj, "transit-c")
	if err != nil {
		t.Fatalf("ensureTenantSNAT: %v", err)
	}
	if conflict != "" {
		t.Fatalf("conflict = %s", conflict)
	}
	// The transit rule wins even though it sorts last — the decision is the
	// subnet, not the order the API happened to list them in.
	if address != "10.199.1.30" {
		t.Errorf("address = %q", address)
	}

	eventually(t, "the losing rule to go", func() error {
		rule := &unstructured.Unstructured{}
		rule.SetGroupVersionKind(ovnSnatGVK)
		err := k8sReader.Get(testCtx, types.NamespacedName{Name: "aaa-external-snat"}, rule)
		if err == nil {
			return fmt.Errorf("it is still there, reporting ready about a NAT " +
				"the router does not have")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})
	// Its address goes with it: otherwise the allocator never hands that one
	// out again.
	eip := &unstructured.Unstructured{}
	eip.SetGroupVersionKind(ovnEipGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "aaa-external-eip"}, eip); err == nil {
		t.Error("the losing address is still held")
	}
	// And the winner's is kept.
	if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "zzz-transit-eip"}, eip); err != nil {
		t.Errorf("the winner's address was taken away: %v", err)
	}
}

// TestAStaleAllowIsDroppedAndAGuessIsNot.
//
// An allow whose address has gone is not untidiness: the address returns to the
// pool, the next tenant is handed it, and inherits a permit to somebody else's
// control-plane ports. Measured on the lab — an allow for .9 survived while .9
// sat free in the transit subnet's available range.
//
// But only when the live set could actually be read. Deleting a permit on a
// guess is how a working tenant loses its control plane.
func TestAStaleAllowIsDroppedAndAGuessIsNot(t *testing.T) {
	rules := []any{
		map[string]any{"priority": int64(transit.DenyPriority), "action": "drop",
			"match": "ip4.src == 10.199.1.0/24"},
		map[string]any{"priority": int64(transit.AllowPriority), "action": "allow-related",
			"match": "ip4.src == 10.199.1.4 && ip4.dst == 10.199.0.101 && tcp.dst == 6443"},
		map[string]any{"priority": int64(transit.AllowPriority), "action": "allow-related",
			"match": "ip4.src == 10.199.1.9 && ip4.dst == 10.199.0.102 && tcp.dst == 6443"},
	}
	live := map[string]struct{}{"10.199.1.4": {}}

	kept := pruneStaleAllows(rules, live, true)
	if len(kept) != 2 {
		t.Fatalf("kept %d rules, want the deny and the live allow", len(kept))
	}
	for _, raw := range kept {
		rule, _ := raw.(map[string]any)
		if strings.Contains(fmt.Sprint(rule["match"]), "10.199.1.9") {
			t.Error("an allow for an address nobody holds survived")
		}
	}

	// A rule this does not understand is somebody else's, and not understanding
	// it is not a reason to delete it.
	foreign := append([]any{}, rules...)
	foreign = append(foreign, map[string]any{
		"priority": int64(transit.AllowPriority), "action": "allow-related",
		"match": "inport == \"some-port\" && tcp.dst == 22",
	})
	if got := pruneStaleAllows(foreign, live, true); len(got) != 3 {
		t.Errorf("kept %d, want the deny, the live allow and the one it cannot "+
			"read", len(got))
	}

	// Could not be read: nothing goes.
	if got := pruneStaleAllows(rules, nil, false); len(got) != 3 {
		t.Errorf("kept %d rules on a guess, want all three", len(got))
	}
	// And the deny is never a candidate, whatever the live set says.
	if got := pruneStaleAllows(rules, map[string]struct{}{}, true); len(got) != 1 {
		t.Errorf("kept %d, want the deny alone", len(got))
	}
}

// TestTheGuardRulesLandOnTheTransitSubnet: the deny once, this tenant's allows
// beside it, and neither duplicated on a second pass.
func TestTheGuardRulesLandOnTheTransitSubnet(t *testing.T) {
	mustTransitSubnet(t, "transit-d")
	obj := vpcTalosTenant("trd")
	obj.Spec.Network = "net-trd"
	reconciler := transitReconciler("transit-d")

	mustEIP(t, "cpt-eip-trd", "transit-d", "10.199.1.31")
	for pass := 1; pass <= 2; pass++ {
		if _, err := reconciler.ensureTransitACLs(testCtx, obj, "transit-d",
			"10.199.0.0/22", "10.199.1.31", "10.199.0.103"); err != nil {
			t.Fatalf("pass %d: %v", pass, err)
		}
	}

	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(ovnSubnetGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "transit-d"}, subnet); err != nil {
		t.Fatalf("reading the transit subnet: %v", err)
	}
	acls, _, _ := unstructured.NestedSlice(subnet.Object, "spec", "acls")

	denies, allows := 0, 0
	ports := map[string]bool{}
	for _, raw := range acls {
		rule, _ := raw.(map[string]any)
		priority, _ := rule["priority"].(int64)
		switch priority {
		case transit.DenyPriority:
			denies++
			// Scoped to what kube-ovn allocates from, not the whole subnet:
			// otherwise the nodes and the control-plane VIP are on the left of
			// a drop rule.
			if fmt.Sprint(rule["match"]) != "ip4.src == {10.199.1.0/24, 10.199.2.0/23}" {
				t.Errorf("deny = %v", rule["match"])
			}
		case transit.AllowPriority:
			allows++
			ports[fmt.Sprint(rule["match"])] = true
		}
	}
	if denies != 1 {
		t.Errorf("denies = %d — it is the baseline every tenant punches through, "+
			"so one per subnet and not one per tenant", denies)
	}
	if allows != 4 {
		t.Errorf("allows = %d, want api, konnectivity, trustd and the clock", allows)
	}
	// The clock is the one that gets forgotten: Talos will not start a kubelet
	// until it is synchronised, so a TCP-only guard presents as a node that
	// never joins.
	if !ports["ip4.src == 10.199.1.31 && ip4.dst == 10.199.0.103 && udp.dst == 123"] {
		t.Errorf("no allow for the clock: %v", ports)
	}
}

// TestARuleWedgedByAMissingAddressIsReleased.
//
// kube-ovn's finalizer wants to unprogram a NAT from the address the rule
// names; when that address is gone the controller loops for ever — the lab's
// rule sat terminating for ten hours, finalizer held, reporting ready the whole
// time. There is then no state left to clean up, so releasing it orphans
// nothing.
//
// A rule whose address still exists keeps its finalizer: that one has work.
func TestARuleWedgedByAMissingAddressIsReleased(t *testing.T) {
	mustTransitSubnet(t, "transit-e")
	reconciler := transitReconciler("transit-e")

	wedged := snatCandidate{name: "wedged-snat", eipName: "gone-eip", deleting: true}
	mustSNATRule(t, "wedged-snat", "net-tre", "net-tre-default", "gone-eip")
	rule := &unstructured.Unstructured{}
	rule.SetGroupVersionKind(ovnSnatGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "wedged-snat"}, rule); err != nil {
		t.Fatalf("reading the rule: %v", err)
	}
	rule.SetFinalizers([]string{"kubeovn.io/ovn-snat-rule"})
	if err := k8sClient.Update(testCtx, rule); err != nil {
		t.Fatalf("planting the finalizer: %v", err)
	}

	if err := reconciler.unwedgeIfTerminating(testCtx, wedged); err != nil {
		t.Fatalf("unwedge: %v", err)
	}
	eventually(t, "the finalizer to be released", func() error {
		live := &unstructured.Unstructured{}
		live.SetGroupVersionKind(ovnSnatGVK)
		if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "wedged-snat"}, live); err != nil {
			if apierrors.IsNotFound(err) {
				return nil
			}
			return err
		}
		if len(live.GetFinalizers()) != 0 {
			return fmt.Errorf("finalizers = %v", live.GetFinalizers())
		}
		return nil
	})

	// One that is not terminating is left alone, whatever its address.
	mustSNATRule(t, "healthy-snat", "net-tre", "net-tre-default", "gone-eip")
	healthy := &unstructured.Unstructured{}
	healthy.SetGroupVersionKind(ovnSnatGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "healthy-snat"}, healthy); err != nil {
		t.Fatalf("reading the rule: %v", err)
	}
	healthy.SetFinalizers([]string{"kubeovn.io/ovn-snat-rule"})
	if err := k8sClient.Update(testCtx, healthy); err != nil {
		t.Fatalf("planting the finalizer: %v", err)
	}
	if err := reconciler.unwedgeIfTerminating(testCtx,
		snatCandidate{name: "healthy-snat", eipName: "gone-eip", deleting: false}); err != nil {
		t.Fatalf("unwedge: %v", err)
	}
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(ovnSnatGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "healthy-snat"}, live); err != nil {
		t.Fatalf("reading the rule: %v", err)
	}
	if len(live.GetFinalizers()) == 0 {
		t.Error("it released a finalizer that still had work to do")
	}
	_ = k8sClient.Delete(testCtx, live)
}

// TestATenantOnTheDefaultOverlayCrossesNoPlane. There is nothing to cross: its
// control plane is a ClusterIP.
func TestATenantOnTheDefaultOverlayCrossesNoPlane(t *testing.T) {
	ready, _, _, err := transitReconciler("transit-a").reconcileTransit(
		testCtx, plainTenant("trf"), "")
	if err != nil {
		t.Fatalf("reconcileTransit: %v", err)
	}
	if !ready {
		t.Error("it waited for a plane the tenant does not use")
	}
}

// TestTheBaselineIsWithheldWhileItWouldSilenceSomebody.
//
// The deny is what every allow punches through, so writing it while another
// tenant's address has no allow does not tighten the plane — it drops that
// tenant's control plane in one patch with nothing saying so.
//
// Not hypothetical. Measured on the stand while this was being written: the
// transit subnet carried **no ACLs at all** while two live tenants held
// addresses inside the allocatable range, so the first write of a correct
// baseline would have taken both control planes down.
func TestTheBaselineIsWithheldWhileItWouldSilenceSomebody(t *testing.T) {
	// Modelled on the live plane at the moment this was written: two foreign
	// tenants holding nat addresses inside the allocatable range, and not one
	// ACL on the subnet.
	mustTransitSubnet(t, "transit-g")
	mustEIP(t, "cpt-eip-stranger-a", "transit-g", "10.199.1.40")
	mustEIP(t, "cpt-eip-stranger-b", "transit-g", "10.199.1.42")
	mustEIP(t, "cpt-eip-trg", "transit-g", "10.199.1.41")

	obj := vpcTalosTenant("trg")
	obj.Spec.Network = "net-trg"
	reconciler := transitReconciler("transit-g")

	unprotected, err := reconciler.ensureTransitACLs(testCtx, obj, "transit-g",
		"10.199.0.0/22", "10.199.1.41", "10.199.0.104")
	if err != nil {
		t.Fatalf("ensureTransitACLs: %v", err)
	}
	if len(unprotected) != 2 ||
		unprotected[0] != "10.199.1.40" || unprotected[1] != "10.199.1.42" {
		t.Fatalf("unprotected = %v, want both strangers", unprotected)
	}

	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(ovnSubnetGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "transit-g"}, subnet); err != nil {
		t.Fatalf("reading the subnet: %v", err)
	}
	acls, _, _ := unstructured.NestedSlice(subnet.Object, "spec", "acls")
	if hasDeny(acls) {
		t.Error("it wrote the baseline and took the stranger's control plane " +
			"with it")
	}
	// Ours is written regardless: withholding the baseline is not withholding
	// the work.
	if len(acls) != 4 {
		t.Errorf("acls = %d, want this tenant's four allows", len(acls))
	}

	// Cover both strangers and the baseline can go in.
	strangerAllows := append(
		transit.Allows("10.199.1.40", "10.199.0.105", []int{6443}, nil),
		transit.Allows("10.199.1.42", "10.199.0.106", []int{6443}, nil)...)
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(ovnSubnetGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "transit-g"}, live); err != nil {
		t.Fatalf("reading the subnet: %v", err)
	}
	current, _, _ := unstructured.NestedSlice(live.Object, "spec", "acls")
	for _, rule := range strangerAllows {
		current = append(current, ruleToMap(rule))
	}
	if err := unstructured.SetNestedSlice(live.Object, current, "spec", "acls"); err != nil {
		t.Fatal(err)
	}
	if err := k8sClient.Update(testCtx, live); err != nil {
		t.Fatalf("granting the stranger: %v", err)
	}

	unprotected, err = reconciler.ensureTransitACLs(testCtx, obj, "transit-g",
		"10.199.0.0/22", "10.199.1.41", "10.199.0.104")
	if err != nil {
		t.Fatalf("ensureTransitACLs: %v", err)
	}
	if len(unprotected) != 0 {
		t.Fatalf("unprotected = %v", unprotected)
	}
	eventually(t, "the baseline", func() error {
		fresh := &unstructured.Unstructured{}
		fresh.SetGroupVersionKind(ovnSubnetGVK)
		if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "transit-g"}, fresh); err != nil {
			return err
		}
		rules, _, _ := unstructured.NestedSlice(fresh.Object, "spec", "acls")
		if !hasDeny(rules) {
			return fmt.Errorf("still withheld with everybody covered")
		}
		return nil
	})
}

// TestARouterPortIsNotATenantAddress.
//
// A VPC's leg holds an address on this subnet too. Counting those as live keeps
// an allow alive after the tenant that owned its address is gone — the address
// having quietly become a router port.
func TestARouterPortIsNotATenantAddress(t *testing.T) {
	mustTransitSubnet(t, "transit-h")
	mustEIP(t, "net-trh-cp-transit", "transit-h", "10.199.1.50")
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(ovnEipGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "net-trh-cp-transit"}, live); err != nil {
		t.Fatalf("reading the eip: %v", err)
	}
	_ = unstructured.SetNestedField(live.Object, "lrp", "spec", "type")
	if err := k8sClient.Update(testCtx, live); err != nil {
		t.Fatalf("making it a router port: %v", err)
	}

	addresses, known := transitReconciler("transit-h").liveTransitAddresses(testCtx, "transit-h")
	if !known {
		t.Fatal("could not read the plane")
	}
	if _, found := addresses["10.199.1.50"]; found {
		t.Error("a router port was counted as a tenant address")
	}
}
