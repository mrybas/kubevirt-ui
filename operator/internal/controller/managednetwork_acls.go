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
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/acl"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/network"
)

// aclOwnerAnnotation marks a subnet whose rule list this controller writes.
//
// Per object, and never assumed. It is set by an adoption step that first
// proves the rendered list already equals the live one, so taking ownership is
// always a no-op on the dataplane.
const aclOwnerAnnotation = "platform.kubevirt-ui.io/acl-owner"

// aclOwnerOperator is the only value that means anything here.
const aclOwnerOperator = "operator"

// systemVPC is kube-ovn's own router; its subnets are not tenant networks and
// take no part in the census.
const systemVPC = "ovn-cluster"

// reconcileACLs composes the rule list and, on subnets this controller owns,
// writes it.
//
// Adoption is the careful part. A live list containing a rule the composer
// cannot reproduce is not adopted: it is named, and the subnet keeps whatever
// wrote it. Silently dropping somebody's rule to take over a list is a worse
// outcome than not taking it over.
func (r *ManagedNetworkReconciler) reconcileACLs(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) error {
	if r.TenantSupernet == "" && network.IsIsolated(net) && net.Spec.Role == "" {
		r.setNetworkCondition(net, platformv1alpha1.ConditionIsolated, false, "NoSupernet",
			"no tenant supernet configured, so there is nothing to scope the "+
				"isolation floor to; a drop without one would take the internet "+
				"with it. Set --tenant-supernet on the operator")
		return nil
	}

	rendered, outOfRange, err := r.composeACLsWithRange(ctx, net)
	if err != nil {
		return err
	}

	name := network.DefaultSubnetName(net)
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: name}, subnet); err != nil {
		r.setNetworkCondition(net, platformv1alpha1.ConditionIsolated, false, "NoSubnet",
			fmt.Sprintf("Subnet/%s does not exist yet", name))
		return nil
	}
	live := readACLs(subnet)

	if subnet.GetAnnotations()[aclOwnerAnnotation] != aclOwnerOperator {
		// A network this controller created has no incumbent: the subnet is its
		// own and the list is empty, so there is nobody to take it from. The
		// adoption dance exists to avoid stealing a list something else
		// maintains, and stealing nothing from nobody is not a risk worth
		// paying for — paying for it here would mean a freshly created network
		// waits for an external pass before it is closed, which is the window
		// this design refuses to have.
		if !(cascadeOnDelete(net) && len(live) == 0) {
			return r.adoptACLs(ctx, net, subnet, live, rendered, outOfRange)
		}
	}

	if acl.Equal(live, rendered) {
		r.reportIsolated(net, rendered, outOfRange)
		return nil
	}

	patched := subnet.DeepCopy()
	if err := writeACLs(patched, rendered); err != nil {
		return err
	}
	annotations := patched.GetAnnotations()
	if annotations == nil {
		annotations = map[string]string{}
	}
	annotations[aclOwnerAnnotation] = aclOwnerOperator
	patched.SetAnnotations(annotations)
	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("writing the rules on Subnet/%s: %w", name, err)
	}
	kube.CountWrite(r.Scheme, patched, networkControllerName, "updated")
	r.reportIsolated(net, rendered, outOfRange)
	return nil
}

// adoptACLs takes ownership only when there is nothing to change.
func (r *ManagedNetworkReconciler) adoptACLs(
	ctx context.Context,
	net *platformv1alpha1.ManagedNetwork,
	subnet *unstructured.Unstructured,
	live, rendered []acl.Rule,
	outOfRange []string,
) error {
	if unaccounted := acl.Unaccounted(live, rendered); len(unaccounted) > 0 {
		described := make([]string, 0, len(unaccounted))
		for _, rule := range unaccounted {
			described = append(described, acl.Describe(rule))
		}
		r.setNetworkCondition(net, platformv1alpha1.ConditionIsolated, false, "NotAdopted",
			fmt.Sprintf("this subnet carries %d rule(s) the composer cannot "+
				"reproduce, so its list is left to whoever wrote it: %s",
				len(unaccounted), strings.Join(described, "; ")))
		return nil
	}

	if !acl.Equal(live, rendered) {
		missing := acl.Unaccounted(rendered, live)
		described := make([]string, 0, len(missing))
		for _, rule := range missing {
			described = append(described, acl.Describe(rule))
		}
		// Everything live is accounted for, but the render adds to it. Adopting
		// here would be a dataplane change disguised as a handover, so it waits
		// for somebody to look.
		r.setNetworkCondition(net, platformv1alpha1.ConditionIsolated, false, "AdoptionWouldChange",
			fmt.Sprintf("taking this list over would add %d rule(s): %s. "+
				"Nothing was written; the handover is only automatic when it "+
				"changes nothing", len(missing), strings.Join(described, "; ")))
		return nil
	}

	patched := subnet.DeepCopy()
	annotations := patched.GetAnnotations()
	if annotations == nil {
		annotations = map[string]string{}
	}
	annotations[aclOwnerAnnotation] = aclOwnerOperator
	patched.SetAnnotations(annotations)
	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("claiming the rules on Subnet/%s: %w", subnet.GetName(), err)
	}
	kube.CountWrite(r.Scheme, patched, networkControllerName, "updated")
	r.reportIsolated(net, rendered, outOfRange)
	return nil
}

func (r *ManagedNetworkReconciler) reportIsolated(
	net *platformv1alpha1.ManagedNetwork, rendered []acl.Rule, outOfRange []string,
) {
	net.Status.Rules = int32(len(rendered))
	if len(outOfRange) > 0 {
		// The aggregate cannot deny what it does not contain, so those are
		// named individually — and said out loud, because a supernet that does
		// not cover the tenants is the exact mistake this design made once.
		sort.Strings(outOfRange)
		r.setNetworkCondition(net, platformv1alpha1.ConditionIsolated, true, "IsolatedWithExceptions",
			fmt.Sprintf("%d rule(s); these tenant networks fall outside %s and "+
				"are denied individually: %s",
				len(rendered), r.TenantSupernet, strings.Join(outOfRange, ", ")))
		return
	}
	if net.Spec.Role != "" {
		r.setNetworkCondition(net, platformv1alpha1.ConditionIsolated, true, "Infrastructure",
			fmt.Sprintf("%d rule(s); this network serves the others, so it takes "+
				"neither the tenant floor nor the management deny", len(rendered)))
		return
	}
	if !network.IsIsolated(net) {
		r.setNetworkCondition(net, platformv1alpha1.ConditionIsolated, false, "NotIsolated",
			fmt.Sprintf("%d rule(s); this network was created open to other "+
				"tenants, deliberately", len(rendered)))
		return
	}
	r.setNetworkCondition(net, platformv1alpha1.ConditionIsolated, true, "Isolated",
		fmt.Sprintf("%d rule(s)", len(rendered)))
}

// composeACLs is the rendered list, or nothing if it cannot be composed.
//
// Errors are swallowed here on purpose: this is called on the create path,
// where the alternative to "no rules yet" is "no subnet yet", and the
// steady-state pass reports the same failure properly a moment later.
func (r *ManagedNetworkReconciler) composeACLs(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) ([]acl.Rule, []string) {
	rendered, outOfRange, err := r.composeACLsWithRange(ctx, net)
	if err != nil {
		return nil, nil
	}
	return rendered, outOfRange
}

func (r *ManagedNetworkReconciler) composeACLsWithRange(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) ([]acl.Rule, []string, error) {
	input, err := r.aclInput(ctx, net)
	if err != nil {
		return nil, nil, err
	}
	rendered, outOfRange := acl.Render(input)
	return rendered, outOfRange, nil
}

// aclInput gathers the sources the list is derived from.
func (r *ManagedNetworkReconciler) aclInput(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) (acl.Input, error) {
	in := acl.Input{
		SubnetCIDR:  net.Spec.CIDR,
		Supernet:    r.TenantSupernet,
		SharedCIDRs: net.Spec.SharedCIDRs,
		Isolated:    network.IsIsolated(net),
		Role:        net.Spec.Role,
	}

	tenants, err := r.tenantCIDRs(ctx)
	if err != nil {
		return in, err
	}
	in.TenantCIDRs = tenants

	peers, err := r.peerCIDRs(ctx, net.Name)
	if err != nil {
		return in, err
	}
	in.PeerCIDRs = peers

	mgmt, err := r.mgmtSources(ctx)
	if err != nil {
		return in, err
	}
	in.MgmtCIDRs = mgmt
	return in, nil
}

// tenantCIDRs is the census: every tenant network, and only tenant networks.
//
// Role is read from the label and never inferred from shape. The census once
// derived it from what an object looked like and counted the egress gateway's
// own VPC as a tenant, which handed every tenant a drop on the very address it
// egresses through.
func (r *ManagedNetworkReconciler) tenantCIDRs(ctx context.Context) ([]string, error) {
	vpcs := &unstructured.UnstructuredList{}
	vpcs.SetGroupVersionKind(vpcGVK.GroupVersion().WithKind("VpcList"))
	if err := r.List(ctx, vpcs); err != nil {
		return nil, fmt.Errorf("listing VPCs for the census: %w", err)
	}
	roles := map[string]string{}
	for i := range vpcs.Items {
		roles[vpcs.Items[i].GetName()] = vpcs.Items[i].GetLabels()[network.RoleLabel]
	}

	subnets := &unstructured.UnstructuredList{}
	subnets.SetGroupVersionKind(subnetGVK.GroupVersion().WithKind("SubnetList"))
	if err := r.List(ctx, subnets); err != nil {
		return nil, fmt.Errorf("listing subnets for the census: %w", err)
	}
	var out []string
	for i := range subnets.Items {
		spec := subnets.Items[i].Object
		vpc, _, _ := unstructured.NestedString(spec, "spec", "vpc")
		if vpc == "" || vpc == systemVPC {
			continue
		}
		if role, ok := roles[vpc]; ok && role != "" {
			// A network that serves the others is not one of them.
			continue
		}
		if cidr, _, _ := unstructured.NestedString(spec, "spec", "cidrBlock"); cidr != "" {
			out = append(out, cidr)
		}
	}
	sort.Strings(out)
	return out, nil
}

// peerCIDRs are the networks this one is peered with.
//
// Read from two places, and the first of them matters more than it looks. A
// declared peering — a ManagedNetworkPeering naming this network — counts
// before anything has been written to any router, which is what lets the allow
// go in *before* the routes. Deriving it only from `Vpc.spec.vpcPeerings` would
// be circular: the peering controller waits for the allow, and the allow waits
// for the peering entry.
//
// The second is the live entries, because the endpoint still writes peerings
// this operator has no object for, and a network composed here can be peered
// from there.
//
// Neither is read back from the ACLs. Deriving the allow list from the list
// being written makes the rules their own source: a rule that should have gone
// stays, because it is still there.
func (r *ManagedNetworkReconciler) peerCIDRs(ctx context.Context, vpc string) ([]string, error) {
	remotes := map[string]bool{}

	declared := &platformv1alpha1.ManagedNetworkPeeringList{}
	if err := r.List(ctx, declared); err != nil {
		return nil, fmt.Errorf("listing declared peerings: %w", err)
	}
	for i := range declared.Items {
		// A peering being deleted still counts, and that is the ordering.
		//
		// The obvious reading — it is going, so stop allowing — takes the allow
		// off the moment the object is *marked*, while the finalizer is still
		// pulling the routes off the routers. That is allow-first,
		// routes-second: for as long as the teardown takes, the traffic is
		// routed at a prefix that now drops it. Exactly the black hole this
		// design spends its effort avoiding, arrived at from the other end.
		//
		// Counting it until the object is actually gone gives the right order
		// for free: the finalizer holds it until the routes are off, and only
		// then does the allow follow.
		// Only what the peering controller has accepted. A declaration is
		// something anybody who can create an object can write; trusting the
		// spec here would let a CR naming two networks open an allow between
		// them even when the peering is refused and no route is ever laid — a
		// hole in the isolation with nothing going through it.
		if accepted := apimeta.FindStatusCondition(
			declared.Items[i].Status.Conditions, platformv1alpha1.ConditionPeeringAccepted,
		); accepted == nil || accepted.Status != metav1.ConditionTrue {
			continue
		}
		networks := declared.Items[i].Spec.Networks
		if len(networks) != 2 {
			continue
		}
		switch vpc {
		case networks[0]:
			remotes[networks[1]] = true
		case networks[1]:
			remotes[networks[0]] = true
		}
	}

	router := &unstructured.Unstructured{}
	router.SetGroupVersionKind(vpcGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: vpc}, router); err == nil {
		peerings, _, _ := unstructured.NestedSlice(router.Object, "spec", "vpcPeerings")
		for _, raw := range peerings {
			entry, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			if remote, _ := entry["remoteVpc"].(string); remote != "" {
				remotes[remote] = true
			}
		}
	}

	if len(remotes) == 0 {
		return nil, nil
	}

	subnets := &unstructured.UnstructuredList{}
	subnets.SetGroupVersionKind(subnetGVK.GroupVersion().WithKind("SubnetList"))
	if err := r.List(ctx, subnets); err != nil {
		return nil, fmt.Errorf("listing subnets for the peer prefixes: %w", err)
	}
	var out []string
	for i := range subnets.Items {
		owner, _, _ := unstructured.NestedString(subnets.Items[i].Object, "spec", "vpc")
		if !remotes[owner] {
			continue
		}
		if cidr, _, _ := unstructured.NestedString(
			subnets.Items[i].Object, "spec", "cidrBlock"); cidr != "" {
			out = append(out, cidr)
		}
	}
	sort.Strings(out)
	return out, nil
}

// mgmtSources is where the management plane is.
//
// Configured wins. Without it, each node's own address as a /32 rather than a
// guessed prefix length: the API reports node addresses, not the network they
// sit on. A previous fallback guessed /24 from the first node it saw and, on a
// cluster whose node network is a /20, covered every node by coincidence — a
// security rule holding for a reason that could change with one new node.
func (r *ManagedNetworkReconciler) mgmtSources(ctx context.Context) ([]string, error) {
	if len(r.MgmtCIDRs) > 0 {
		return r.MgmtCIDRs, nil
	}
	nodes := &corev1.NodeList{}
	if err := r.List(ctx, nodes); err != nil {
		return nil, fmt.Errorf("listing nodes for the management deny: %w", err)
	}
	var out []string
	for i := range nodes.Items {
		for _, address := range nodes.Items[i].Status.Addresses {
			if address.Type == corev1.NodeInternalIP && address.Address != "" {
				out = append(out, address.Address+"/32")
			}
		}
	}
	sort.Strings(out)
	return out, nil
}

func readACLs(subnet *unstructured.Unstructured) []acl.Rule {
	raw, _, _ := unstructured.NestedSlice(subnet.Object, "spec", "acls")
	out := make([]acl.Rule, 0, len(raw))
	for _, item := range raw {
		entry, ok := item.(map[string]any)
		if !ok {
			continue
		}
		priority := 0
		switch value := entry["priority"].(type) {
		case int64:
			priority = int(value)
		case float64:
			priority = int(value)
		}
		action, _ := entry["action"].(string)
		direction, _ := entry["direction"].(string)
		match, _ := entry["match"].(string)
		out = append(out, acl.Rule{
			Action: action, Direction: direction, Match: match, Priority: priority,
		})
	}
	return out
}

func writeACLs(subnet *unstructured.Unstructured, rules []acl.Rule) error {
	out := make([]any, 0, len(rules))
	for _, rule := range rules {
		out = append(out, map[string]any{
			"action":    rule.Action,
			"direction": rule.Direction,
			"match":     rule.Match,
			"priority":  int64(rule.Priority),
		})
	}
	return unstructured.SetNestedSlice(subnet.Object, out, "spec", "acls")
}
