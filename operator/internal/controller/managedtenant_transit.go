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
	"context"
	"fmt"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/tenant"
	"github.com/mrybas/kubevirt-ui/operator/internal/transit"
)

// vpcGVK and ovnEipGVK are declared beside the controllers that first needed
// them; only the SNAT rule is new here.
var ovnSnatGVK = schema.GroupVersionKind{
	Group: "kubeovn.io", Version: "v1", Kind: "OvnSnatRule",
}

// reconcileTransit builds the tenant's path to its own control plane.
//
// Three parts, and the order is the point. The VPC gets a port on the transit
// subnet **and the policy route that protects it in the same write** — policy
// routes beat static ones, so an egress gateway's catch-all would otherwise
// swallow the packets going one hop to the control plane. Then the tenant's
// subnet is given an address on that subnet to leave under, because the reply
// has to come back to something the nodes know on the transit bridge. Then the
// guard ACLs let that address, and only it, reach that VIP on those ports.
//
// Why any of it: the plane has no gateway leg on it at all. An egress gateway
// falling over takes the internet with it and leaves the control plane — and
// the CSI path to the host API, which rides the same leg — untouched.
func (r *ManagedTenantReconciler) reconcileTransit(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, vip string,
) (ready bool, reason, message string, err error) {
	if obj.Spec.Network == "" || vip == "" {
		// A tenant on the default overlay reaches its control plane by
		// ClusterIP; there is no plane to cross.
		return true, "", "", nil
	}
	subnetName := r.transitSubnet()
	if subnetName == "" {
		return false, "TransitNotConfigured",
			"no transit subnet is configured, so a worker in a VPC has no path " +
				"to its own control plane", nil
	}

	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(ovnSubnetGVK)
	if err := r.reader().Get(ctx, types.NamespacedName{Name: subnetName}, subnet); err != nil {
		if unreadable(err) {
			return false, "TransitMissing", fmt.Sprintf(
				"the transit subnet %q does not exist", subnetName), nil
		}
		return false, "", "", fmt.Errorf("reading the transit subnet: %w", err)
	}
	transitCIDR, _, _ := unstructured.NestedString(subnet.Object, "spec", "cidrBlock")
	if transitCIDR == "" {
		return false, "TransitMissing", fmt.Sprintf(
			"the transit subnet %q has no cidrBlock", subnetName), nil
	}

	if err := r.attachToTransit(ctx, obj.Spec.Network, subnetName, transitCIDR); err != nil {
		return false, "", "", err
	}

	address, conflict, err := r.ensureTenantSNAT(ctx, obj, subnetName)
	if err != nil {
		return false, "", "", err
	}
	if conflict != "" {
		// Not transient and not self-healing: another rule owns the one SNAT
		// slot this subnet has, on a network the control-plane path cannot use.
		// Saying so is the whole value — the cluster comes up looking fine and
		// only the control plane is dead.
		return false, "TransitSnatSlotTaken", conflict, nil
	}
	if address == "" {
		return false, "WaitingForTransitAddress", fmt.Sprintf(
			"waiting for kube-ovn to give %s an address on %s", obj.Name, subnetName), nil
	}

	unprotected, err := r.ensureTransitACLs(ctx, obj, subnetName, transitCIDR, address, vip)
	if err != nil {
		return false, "", "", err
	}
	if len(unprotected) > 0 {
		// The tenant's own path is wired; the plane is not closed. Said out
		// loud because an open transit plane is not something to discover from
		// an ACL listing at three in the morning.
		return true, "WiredBaselineWithheld", fmt.Sprintf(
			"%s leaves under %s on %s. The baseline deny is withheld: %s hold "+
				"addresses here with no allow, and writing it would drop them.",
			obj.Name, address, subnetName, strings.Join(unprotected, ", ")), nil
	}
	return true, "Wired", fmt.Sprintf("%s leaves under %s on %s", obj.Name, address, subnetName), nil
}

// attachToTransit gives the VPC a router port on the plane, with its guard.
func (r *ManagedTenantReconciler) attachToTransit(
	ctx context.Context, vpcName, subnetName, transitCIDR string,
) error {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(vpcGVK)
	live.SetName(vpcName)
	guard := transit.Guard(transitCIDR)

	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		externals, _, _ := unstructured.NestedStringSlice(live.Object, "spec", "extraExternalSubnets")
		if !containsName(externals, subnetName) {
			externals = append(externals, subnetName)
		}
		policies, _, _ := unstructured.NestedSlice(live.Object, "spec", "policyRoutes")
		if !hasGuard(policies, transitCIDR) {
			policies = append(policies, guard)
		}
		if err := unstructured.SetNestedField(live.Object, true, "spec", "enableExternal"); err != nil {
			return err
		}
		if err := unstructured.SetNestedStringSlice(live.Object, externals,
			"spec", "extraExternalSubnets"); err != nil {
			return err
		}
		// Written in the same patch as the attachment, deliberately. Attached
		// without the guard, the VPC has a leg it cannot use for the one thing
		// the leg is for.
		return unstructured.SetNestedSlice(live.Object, policies, "spec", "policyRoutes")
	})
	if err != nil {
		return fmt.Errorf("attaching %s to %s: %w", vpcName, subnetName, err)
	}
	return nil
}

func containsName(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}

func hasGuard(policies []any, transitCIDR string) bool {
	want := "ip4.dst == " + transitCIDR
	for _, raw := range policies {
		policy, _ := raw.(map[string]any)
		if policy == nil {
			continue
		}
		priority, _ := policy["priority"].(int64)
		if priority == transit.GuardPriority && policy["match"] == want {
			return true
		}
	}
	return false
}

// snatCandidate is one rule claiming the SNAT slot for a tenant's subnet.
type snatCandidate struct {
	name     string
	labels   map[string]string
	eipName  string
	address  string
	subnet   string
	deleting bool
	dangling bool
}

// ensureTenantSNAT gives the tenant's subnet an address on the transit plane.
//
// Returns the address, or a conflict to report. The decision is made on the
// **whole set** of rules claiming the subnet, never on the first match: OVN
// keeps one SNAT per logical IP, so two rules cannot both be in force, and the
// loser reports `ready: true` about a NAT the router does not have. The lab
// carried exactly that for days, two ready rules and one of them fictional.
func (r *ManagedTenantReconciler) ensureTenantSNAT(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, transitSubnet string,
) (address, conflict string, err error) {
	tenantSubnet := obj.Spec.Network + "-default"
	ours := "cpt-snat-" + obj.Name
	eipName := "cpt-eip-" + obj.Name

	candidates, dangling, err := r.snatRulesCovering(ctx, obj.Spec.Network, tenantSubnet, ours)
	if err != nil {
		return "", "", err
	}

	var onTransit, elsewhere []snatCandidate
	for _, candidate := range candidates {
		if candidate.subnet == transitSubnet {
			onTransit = append(onTransit, candidate)
			continue
		}
		elsewhere = append(elsewhere, candidate)
	}

	if len(onTransit) == 0 {
		if len(elsewhere) > 0 {
			first := elsewhere[0]
			// Reusing this would write the guard ACLs for an address on another
			// network: it looks configured and works for nothing. Measured on
			// the lab as the tenant whose internet was fine and whose control
			// plane was unreachable.
			return "", fmt.Sprintf(
				"the SNAT slot for %s is held by %q → %s on subnet %q, which is "+
					"not the control-plane transit network. Remove that rule, and "+
					"the VPC's NAT gateway that created it, before attaching a "+
					"tenant.", tenantSubnet, first.name, first.address, first.subnet), nil
		}
		return r.createTenantSNAT(ctx, obj, eipName, ours, tenantSubnet, transitSubnet)
	}

	winner := onTransit[0]
	losers := append(append([]snatCandidate{}, onTransit[1:]...), elsewhere...)
	for _, loser := range append(losers, dangling...) {
		if err := r.dropLosingRule(ctx, loser, winner.eipName); err != nil {
			return "", "", err
		}
	}

	// Reusing a rule is only safe if it is actually in force, and `ready` does
	// not say that. Cycling a VPC's transit attachment — which is what deleting
	// the last tenant does — leaves the rule reporting ready while the router
	// has no NAT at all, and kube-ovn never recovers: it loops trying to remove
	// a NAT that is not there and never gets as far as creating one. Recreating
	// is the only exit, and the cost — a momentary SNAT reset for this VPC — is
	// worth paying against a worker that can never reach its control plane.
	if err := r.deleteIfPresent(ctx, ovnSnatGVK, winner.name); err != nil {
		return "", "", err
	}
	if err := r.createSNATRule(ctx, winner.name, winner.labels,
		winner.eipName, obj.Spec.Network, tenantSubnet); err != nil {
		return "", "", err
	}
	return winner.address, "", nil
}

// snatRulesCovering is every rule claiming this (vpc, subnet), sorted by name.
//
// Sorted so a cluster carrying two resolves the same way on every pass:
// "whichever the API listed first" is not a tie-break anyone can reason about
// afterwards. A rule whose EIP does not exist comes back separately — it cannot
// possibly be programmed, yet reports ready like any other — and is not deleted
// on sight, because during a create the EIP may simply not exist yet.
func (r *ManagedTenantReconciler) snatRulesCovering(
	ctx context.Context, vpcName, tenantSubnet, skip string,
) (found, dangling []snatCandidate, err error) {
	rules := &unstructured.UnstructuredList{}
	rules.SetGroupVersionKind(ovnSnatGVK.GroupVersion().WithKind("OvnSnatRuleList"))
	if err := r.reader().List(ctx, rules); err != nil {
		if unreadable(err) {
			return nil, nil, nil
		}
		return nil, nil, fmt.Errorf("listing SNAT rules: %w", err)
	}

	items := rules.Items
	sort.Slice(items, func(i, j int) bool { return items[i].GetName() < items[j].GetName() })

	for i := range items {
		item := &items[i]
		if item.GetName() == skip {
			continue
		}
		vpc, _, _ := unstructured.NestedString(item.Object, "spec", "vpc")
		subnet, _, _ := unstructured.NestedString(item.Object, "spec", "vpcSubnet")
		if vpc != vpcName || subnet != tenantSubnet {
			continue
		}
		eipName, _, _ := unstructured.NestedString(item.Object, "spec", "ovnEip")
		if eipName == "" {
			continue
		}
		candidate := snatCandidate{
			name:     item.GetName(),
			labels:   item.GetLabels(),
			eipName:  eipName,
			deleting: item.GetDeletionTimestamp() != nil,
			subnet:   "<unknown>",
		}
		eip := &unstructured.Unstructured{}
		eip.SetGroupVersionKind(ovnEipGVK)
		if err := r.reader().Get(ctx, types.NamespacedName{Name: eipName}, eip); err == nil {
			candidate.address, _, _ = unstructured.NestedString(eip.Object, "status", "v4Ip")
			if external, _, _ := unstructured.NestedString(eip.Object, "spec", "externalSubnet"); external != "" {
				candidate.subnet = external
			}
		}
		if candidate.address == "" {
			candidate.dangling = true
			dangling = append(dangling, candidate)
			continue
		}
		found = append(found, candidate)
	}
	return found, dangling, nil
}

// createTenantSNAT gives this tenant its own address on the plane.
func (r *ManagedTenantReconciler) createTenantSNAT(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	eipName, ruleName, tenantSubnet, transitSubnet string,
) (address, conflict string, err error) {
	labels := map[string]string{
		"kubevirt-ui.io/managed": "true",
		"kubevirt-ui.io/tenant":  obj.Name,
	}
	eip := &unstructured.Unstructured{}
	eip.SetGroupVersionKind(ovnEipGVK)
	eip.SetName(eipName)
	eipLabels := map[string]string{"kubevirt-ui.io/vpc": obj.Spec.Network}
	for key, value := range labels {
		eipLabels[key] = value
	}
	eip.SetLabels(eipLabels)
	if err := unstructured.SetNestedMap(eip.Object, map[string]any{
		"externalSubnet": transitSubnet, "type": "nat",
	}, "spec"); err != nil {
		return "", "", err
	}
	if err := r.createIgnoringConflict(ctx, eip); err != nil {
		return "", "", err
	}

	if err := r.createSNATRule(ctx, ruleName, labels, eipName,
		obj.Spec.Network, tenantSubnet); err != nil {
		return "", "", err
	}

	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(ovnEipGVK)
	if err := r.reader().Get(ctx, types.NamespacedName{Name: eipName}, live); err != nil {
		// Not an error: the address arrives when kube-ovn allocates it, and the
		// ACLs that need it are written on the pass that sees it.
		return "", "", nil
	}
	address, _, _ = unstructured.NestedString(live.Object, "status", "v4Ip")
	return address, "", nil
}

func (r *ManagedTenantReconciler) createSNATRule(
	ctx context.Context, name string, labels map[string]string,
	eipName, vpcName, tenantSubnet string,
) error {
	rule := &unstructured.Unstructured{}
	rule.SetGroupVersionKind(ovnSnatGVK)
	rule.SetName(name)
	rule.SetLabels(labels)
	if err := unstructured.SetNestedMap(rule.Object, map[string]any{
		// `vpc` is mandatory — kube-ovn resolves the logical router from it,
		// and a rule without one never leaves "failed to get vpc for snat".
		"ovnEip": eipName, "vpc": vpcName, "vpcSubnet": tenantSubnet,
	}, "spec"); err != nil {
		return err
	}
	return r.createIgnoringConflict(ctx, rule)
}

// dropLosingRule removes a rule that cannot be in force, and the address it was
// holding.
//
// Not untidiness: the guard ACLs are keyed on an address, so two ready rules
// leave the next person unable to tell which address the tenant actually leaves
// under. The EIP goes with it — it is holding a transit address the allocator
// would otherwise never hand out again — unless the winner is using it.
func (r *ManagedTenantReconciler) dropLosingRule(
	ctx context.Context, loser snatCandidate, keepEIP string,
) error {
	if err := r.deleteIfPresent(ctx, ovnSnatGVK, loser.name); err != nil {
		return err
	}
	if err := r.unwedgeIfTerminating(ctx, loser); err != nil {
		return err
	}
	if loser.eipName != "" && loser.eipName != keepEIP {
		return r.deleteIfPresent(ctx, ovnEipGVK, loser.eipName)
	}
	return nil
}

// unwedgeIfTerminating releases a rule whose finalizer can never complete.
//
// Deleting is not enough when somebody already deleted it: kube-ovn's finalizer
// wants to unprogram a NAT from the EIP the rule names, and when that EIP is
// gone the controller loops for ever — the lab's rule sat like that for ten
// hours, deletionTimestamp set, finalizer held, `ready: true` throughout.
//
// **Only** for a rule already terminating whose EIP is missing. There is then no
// state left for the finalizer to clean up, so removing it orphans nothing; a
// rule with a live EIP keeps its finalizer, because that one has real work.
func (r *ManagedTenantReconciler) unwedgeIfTerminating(
	ctx context.Context, loser snatCandidate,
) error {
	if !loser.deleting {
		return nil
	}
	eip := &unstructured.Unstructured{}
	eip.SetGroupVersionKind(ovnEipGVK)
	if err := r.reader().Get(ctx, types.NamespacedName{Name: loser.eipName}, eip); err == nil {
		return nil
	}
	rule := &unstructured.Unstructured{}
	rule.SetGroupVersionKind(ovnSnatGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: loser.name}, rule); err != nil {
		return client.IgnoreNotFound(err)
	}
	rule.SetFinalizers(nil)
	if err := r.Update(ctx, rule); err != nil {
		return client.IgnoreNotFound(err)
	}
	kube.CountWrite(r.Scheme, rule, tenantControllerName, "updated")
	return nil
}

func (r *ManagedTenantReconciler) createIgnoringConflict(
	ctx context.Context, obj *unstructured.Unstructured,
) error {
	if err := r.Create(ctx, obj); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return nil
		}
		return fmt.Errorf("creating %s %s: %w", obj.GetKind(), obj.GetName(), err)
	}
	kube.CountWrite(r.Scheme, obj, tenantControllerName, "created")
	return nil
}

func (r *ManagedTenantReconciler) deleteIfPresent(
	ctx context.Context, gvk schema.GroupVersionKind, name string,
) error {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(gvk)
	obj.SetName(name)
	if err := r.Delete(ctx, obj); err != nil {
		return client.IgnoreNotFound(err)
	}
	kube.CountWrite(r.Scheme, obj, tenantControllerName, "deleted")
	return nil
}

// ensureTransitACLs writes this tenant's allows, and the deny they are
// exceptions to.
//
// The deny is written once for the subnet and left alone: it is the baseline
// every tenant punches through, so it must not be duplicated per tenant and
// must not vanish when one is removed.
//
// Every write also drops allows whose source no longer belongs to any address
// on this plane. Removal used to happen only at delete time, so a tenant whose
// address had already gone left its allows behind — and that is not untidiness
// but a hole: the address returns to the pool, the next tenant is handed it, and
// inherits a permit to somebody else's control-plane ports.
func (r *ManagedTenantReconciler) ensureTransitACLs(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	subnetName, transitCIDR, address, vip string,
) (unprotected []string, err error) {
	tcp := []int{tenantAPIPort, tenantKonnPort}
	if obj.Spec.Workers.OS == "talos" {
		tcp = append(tcp, tenantTrustdPort)
	}
	wanted := transit.Allows(address, vip, tcp, []int{ntpPort})

	live, known := r.liveTransitAddresses(ctx, subnetName)
	if known {
		// Ours may not be observable yet.
		live[address] = obj.Name
	}

	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(ovnSubnetGVK)
	subnet.SetName(subnetName)
	_, err = kube.Ensure(ctx, r.Client, tenantControllerName, subnet, func() error {
		existing, _, _ := unstructured.NestedSlice(subnet.Object, "spec", "acls")

		acls := pruneStaleAllows(existing, live, known)

		for _, rule := range wanted {
			candidate := ruleToMap(rule)
			if !hasRule(acls, candidate) {
				acls = append(acls, candidate)
			}
		}

		// The deny goes in only once everybody on the plane is covered.
		//
		// It is the baseline the allows punch through, so writing it while some
		// other tenant's address has no allow does not tighten the plane — it
		// drops that tenant's control plane, in one patch, with nothing saying
		// so. Measured on this stand: the subnet carries no ACLs at all while
		// two live tenants hold addresses in the allocatable range, so the very
		// first write of a correct baseline would have taken both down.
		//
		// Withholding leaves the plane exactly as open as it already is, which
		// is not a regression, and names what is missing.
		// Whoever else is on this plane without a permission gets one written
		// for them first, from what their own Service says. The baseline can
		// then go in without taking them down; anything that cannot be
		// attributed keeps it withheld.
		if missing := addressesWithoutAllow(acls, live, known); len(missing) > 0 {
			var backfilled []transit.Rule
			backfilled, unprotected = r.backfillAllows(ctx, missing, live)
			for _, rule := range backfilled {
				candidate := ruleToMap(rule)
				if !hasRule(acls, candidate) {
					acls = append(acls, candidate)
				}
			}
		} else {
			unprotected = nil
		}
		if len(unprotected) == 0 && !hasDeny(acls) {
			excludes, _, _ := unstructured.NestedStringSlice(subnet.Object, "spec", "excludeIps")
			acls = append(acls, ruleToMap(transit.Deny(transitCIDR, excludes)))
		}
		return unstructured.SetNestedSlice(subnet.Object, acls, "spec", "acls")
	})
	if err != nil {
		return nil, fmt.Errorf("writing the transit guard for %s: %w", obj.Name, err)
	}
	return unprotected, nil
}

// addressesWithoutAllow is who is on the plane with no permission written for
// them — the tenants a baseline deny would silence.
func addressesWithoutAllow(acls []any, live map[string]string, known bool) []string {
	if !known {
		// Cannot tell who is here, so cannot promise the baseline is safe.
		return []string{"<the addresses on this plane could not be read>"}
	}
	allowed := map[string]struct{}{}
	for _, raw := range acls {
		rule, _ := raw.(map[string]any)
		if rule == nil {
			continue
		}
		if priority, _ := rule["priority"].(int64); priority != transit.AllowPriority {
			continue
		}
		match, _ := rule["match"].(string)
		if source := transit.AllowSource(match); source != "" {
			allowed[source] = struct{}{}
		}
	}
	var missing []string
	for address := range live {
		if _, covered := allowed[address]; !covered {
			missing = append(missing, address)
		}
	}
	sort.Strings(missing)
	return missing
}

// pruneStaleAllows drops allows whose source no longer belongs to any address
// on the plane.
//
// `known` is whether the live set could be read at all. When it could not,
// nothing is dropped: deleting a permit on a guess is how a working tenant
// loses its control plane, and the rules can wait for a pass that can see.
func pruneStaleAllows(existing []any, live map[string]string, known bool) []any {
	var out []any
	for _, raw := range existing {
		rule, _ := raw.(map[string]any)
		if rule == nil {
			continue
		}
		priority, _ := rule["priority"].(int64)
		if known && priority == transit.AllowPriority {
			match, _ := rule["match"].(string)
			source := transit.AllowSource(match)
			// An allow whose source cannot be read is somebody else's rule in a
			// shape this does not know. Dropping it would be deciding, from not
			// understanding it, that it does not matter.
			if source != "" {
				if _, alive := live[source]; !alive {
					continue
				}
			}
		}
		out = append(out, rule)
	}
	return out
}

// liveTransitAddresses are the tenant addresses currently held on the plane.
// The bool is whether it could be told at all — the caller must leave the rules
// alone rather than delete on a guess.
//
// `nat` only. A router port also holds an address on this subnet, and counting
// those would keep an allow alive after the tenant that owned its address is
// gone — the address it names having quietly become a router leg.
func (r *ManagedTenantReconciler) liveTransitAddresses(
	ctx context.Context, subnetName string,
) (map[string]string, bool) {
	eips := &unstructured.UnstructuredList{}
	eips.SetGroupVersionKind(ovnEipGVK.GroupVersion().WithKind("OvnEipList"))
	if err := r.reader().List(ctx, eips); err != nil {
		return nil, false
	}
	// address -> the tenant it belongs to, "" when it cannot be attributed.
	live := map[string]string{}
	for i := range eips.Items {
		item := &eips.Items[i]
		if external, _, _ := unstructured.NestedString(item.Object, "spec", "externalSubnet"); external != subnetName {
			continue
		}
		if kind, _, _ := unstructured.NestedString(item.Object, "spec", "type"); kind != "nat" {
			continue
		}
		if addr, _, _ := unstructured.NestedString(item.Object, "status", "v4Ip"); addr != "" {
			live[addr] = tenantOfEIP(item)
		}
	}
	return live, true
}

// tenantOfEIP is whose address this is, from the label if it carries one and
// from the name this operator and the product both use otherwise. Empty when
// neither says.
func tenantOfEIP(eip *unstructured.Unstructured) string {
	if name := eip.GetLabels()["kubevirt-ui.io/tenant"]; name != "" {
		return name
	}
	return strings.TrimPrefix(eip.GetName(), "cpt-eip-")
}

func hasDeny(acls []any) bool {
	for _, raw := range acls {
		rule, _ := raw.(map[string]any)
		if rule == nil {
			continue
		}
		priority, _ := rule["priority"].(int64)
		if rule["action"] == "drop" && priority == transit.DenyPriority {
			return true
		}
	}
	return false
}

func hasRule(acls []any, wanted map[string]any) bool {
	for _, raw := range acls {
		rule, _ := raw.(map[string]any)
		if rule == nil {
			continue
		}
		if rule["action"] == wanted["action"] && rule["match"] == wanted["match"] &&
			rule["direction"] == wanted["direction"] &&
			fmt.Sprint(rule["priority"]) == fmt.Sprint(wanted["priority"]) {
			return true
		}
	}
	return false
}

func ruleToMap(rule transit.Rule) map[string]any {
	return map[string]any{
		"action":    rule.Action,
		"direction": rule.Direction,
		"priority":  int64(rule.Priority),
		"match":     rule.Match,
	}
}

func transitCondition(ready bool, reason, message string) metav1.Condition {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	if reason == "" {
		reason = "Wiring"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionTransitReady,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}

// backfillAllows writes the permissions of tenants that are on the plane
// without one — so the baseline can go in without silencing them.
//
// Attributed, never guessed. The address names its tenant (by label, or by the
// name both writers use), and everything else is read off that tenant's own
// control-plane Service: the address it answers on, and the ports it publishes.
// A Talos tenant's Service carries trustd, a cloud-init one's does not, and the
// clock is there if its time Service exists. Nothing is inferred from a shape
// this operator believes the tenant ought to have.
//
// What cannot be attributed is left alone and reported. Writing a permission
// for an address whose owner cannot be identified is how a plane ends up with
// rules nobody can account for.
func (r *ManagedTenantReconciler) backfillAllows(
	ctx context.Context, missing []string, live map[string]string,
) (rules []transit.Rule, unattributable []string) {
	for _, address := range missing {
		tenantName := live[address]
		if tenantName == "" {
			unattributable = append(unattributable, address)
			continue
		}
		vip, tcp, ok := r.controlPlaneShapeOf(ctx, tenantName)
		if !ok {
			unattributable = append(unattributable, address)
			continue
		}
		var udp []int
		if r.servesTime(ctx, tenantName) {
			udp = append(udp, ntpPort)
		}
		rules = append(rules, transit.Allows(address, vip, tcp, udp)...)
	}
	return rules, unattributable
}

// controlPlaneShapeOf reads a tenant's address and ports off the Service it
// answers on, rather than deciding what they ought to be.
func (r *ManagedTenantReconciler) controlPlaneShapeOf(
	ctx context.Context, tenantName string,
) (vip string, tcp []int, ok bool) {
	service := &corev1.Service{}
	if err := r.reader().Get(ctx, types.NamespacedName{
		Namespace: tenant.NamespaceOf(tenantName), Name: tenantName + "-cp-lb",
	}, service); err != nil {
		return "", nil, false
	}
	for _, ingress := range service.Status.LoadBalancer.Ingress {
		if ingress.IP != "" {
			vip = ingress.IP
			break
		}
	}
	if vip == "" {
		return "", nil, false
	}
	for _, port := range service.Spec.Ports {
		if port.Protocol == corev1.ProtocolUDP {
			continue
		}
		tcp = append(tcp, int(port.Port))
	}
	return vip, tcp, len(tcp) > 0
}

func (r *ManagedTenantReconciler) servesTime(ctx context.Context, tenantName string) bool {
	service := &corev1.Service{}
	err := r.reader().Get(ctx, types.NamespacedName{
		Namespace: ntpNamespace(), Name: tenantName + "-ntp",
	}, service)
	return err == nil
}
