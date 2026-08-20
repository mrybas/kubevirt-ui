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
	"net/netip"
	"sort"
	"strings"

	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/acl"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/peering"
)

const (
	peeringControllerName = "managednetworkpeering"

	// peeringFinalizer holds the object long enough to take both ends off.
	// Unlike a network, a peering owns no addresses and nothing runs on it, so
	// removing it on delete is unambiguously the right thing.
	peeringFinalizer = "platform.kubevirt-ui.io/peering"
)

// ManagedNetworkPeeringReconciler writes both ends of a peering, or neither.
//
// The endpoint this replaces held the list of applied ends in a local variable
// and undid them in an `except ApiException` block. That covers the failure it
// was written for and not the one that matters: a process that stops between
// the two writes leaves a peering configured on one side only, which is a black
// hole — the other router has no route back — and nothing anywhere remembers to
// undo it. Here the record is in status, written before each end is attempted,
// so the undo survives whatever happens to the process.
type ManagedNetworkPeeringReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managednetworkpeerings,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managednetworkpeerings/status,verbs=get;update;patch

// Reconcile brings both ends into line with the declaration.
func (r *ManagedNetworkPeeringReconciler) Reconcile(
	ctx context.Context, req ctrl.Request,
) (ctrl.Result, error) {
	link := &platformv1alpha1.ManagedNetworkPeering{}
	if err := r.Get(ctx, req.NamespacedName, link); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !link.DeletionTimestamp.IsZero() {
		return r.tearDownPeering(ctx, link)
	}
	if link.Annotations[pausedAnnotation] == "true" {
		return ctrl.Result{}, nil
	}

	if !controllerutil.ContainsFinalizer(link, peeringFinalizer) {
		controllerutil.AddFinalizer(link, peeringFinalizer)
		if err := r.Update(ctx, link); err != nil {
			return ctrl.Result{}, fmt.Errorf("claiming the peering: %w", err)
		}
		kube.CountWrite(r.Scheme, link, peeringControllerName, "updated")
		return ctrl.Result{Requeue: true}, nil
	}

	before := link.DeepCopy()
	a, b := link.Spec.Networks[0], link.Spec.Networks[1]

	// Both routers are checked before either is written. The rollback below
	// exists for the race where one disappears mid-write, but using it as the
	// normal path means the first end is written and taken off again on every
	// retry — a black hole that flaps every few seconds instead of one that
	// stays. Caught by a test watching longer than the retry interval.
	for _, name := range []string{a, b} {
		vpc := &unstructured.Unstructured{}
		vpc.SetGroupVersionKind(vpcGVK)
		err := r.Get(ctx, types.NamespacedName{Name: name}, vpc)
		if apierrors.IsNotFound(err) {
			r.setAcceptedCondition(link, false, "NoSuchNetwork",
				fmt.Sprintf("there is no Vpc/%s", name))
			r.setPeeringCondition(link, false, "NoSuchNetwork",
				fmt.Sprintf("there is no Vpc/%s to write an end on; nothing was "+
					"written on the other side either", name))
			link.Status.ObservedGeneration = link.Generation
			return ctrl.Result{RequeueAfter: drainRetry},
				kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before)
		}
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("reading Vpc/%s: %w", name, err)
		}
		if !vpc.GetDeletionTimestamp().IsZero() {
			r.setAcceptedCondition(link, false, "NetworkGoing",
				fmt.Sprintf("Vpc/%s is being deleted", name))
			r.setPeeringCondition(link, false, "NetworkGoing",
				fmt.Sprintf("Vpc/%s is being deleted; a peering onto it would "+
					"outlive the thing it points at", name))
			link.Status.ObservedGeneration = link.Generation
			return ctrl.Result{RequeueAfter: drainRetry},
				kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before)
		}
	}

	cidrs := map[string][]string{}
	for _, name := range []string{a, b} {
		found, err := r.subnetCIDRs(ctx, name)
		if err != nil {
			return ctrl.Result{}, err
		}
		if len(found) == 0 {
			r.setAcceptedCondition(link, false, "NoSubnets",
				fmt.Sprintf("%s has no subnet", name))
			r.setPeeringCondition(link, false, "NoSubnets",
				fmt.Sprintf("%s has no subnet, so there would be nothing to route "+
					"to; both networks need one before they can be peered", name))
			link.Status.ObservedGeneration = link.Generation
			return ctrl.Result{RequeueAfter: drainRetry},
				kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before)
		}
		cidrs[name] = found
	}

	// Refuse before writing anything if the rules on either side will never let
	// the traffic through.
	//
	// Routing into a drop is worse than not peering: before, the traffic took
	// the default route out through the gateway and came back; after, it goes
	// to the link and dies. That is fine for the seconds a composer takes to
	// lift its own drop, and permanent when the rule list belongs to something
	// that will never hear about this peering.
	reason, detail := r.isolationWillNotLift(ctx, a, b, cidrs)
	if reason == "IsolationNotOurs" || reason == "Unreadable" {
		// Not accepted, so the composer must not open anything either — an
		// allow with no route through it is a hole in the isolation and nothing
		// to show for it. Anything an earlier pass wrote comes off.
		r.setAcceptedCondition(link, false, reason, detail)
		r.rollBack(ctx, link)
		r.setPeeringCondition(link, false, reason, detail)
		r.setTrafficCondition(link, false, reason, detail)
		link.Status.ObservedGeneration = link.Generation
		return ctrl.Result{RequeueAfter: drainRetry},
			kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before)
	}

	// Accepted: the composer may now open the prefix. Written before the wait
	// below, because that wait is *for* the composer to act on this.
	r.setAcceptedCondition(link, true, "Accepted",
		fmt.Sprintf("%s and %s both exist, both have something to route to, and "+
			"their rules are open or composed", a, b))
	if err := kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before); err != nil {
		return ctrl.Result{}, err
	}
	before = link.DeepCopy()

	if reason != "" {
		r.setPeeringCondition(link, false, reason, detail)
		r.setTrafficCondition(link, false, reason, detail)
		link.Status.ObservedGeneration = link.Generation
		return ctrl.Result{RequeueAfter: drainRetry},
			kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before)
	}

	chosen, err := r.chooseLink(ctx, link)
	if err != nil {
		r.setPeeringCondition(link, false, "NoLink", err.Error())
		link.Status.ObservedGeneration = link.Generation
		return ctrl.Result{RequeueAfter: drainRetry},
			kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before)
	}

	// The plan is recorded before anything is written, and that is the point of
	// it: what has to be undone is what was *attempted*, not what is known to
	// have landed. A process that stops mid-write leaves no report of the
	// second, and removing an end that was never written costs nothing.
	planned := []platformv1alpha1.PeeringLeg{
		{Network: a, ConnectIP: chosen.A},
		{Network: b, ConnectIP: chosen.B},
	}
	if link.Status.LinkCIDR != chosen.CIDR || !samePlan(link.Status.Legs, planned) {
		link.Status.LinkCIDR = chosen.CIDR
		link.Status.Legs = planned
		if err := kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before); err != nil {
			return ctrl.Result{}, err
		}
		before = link.DeepCopy()
	}

	ends := [][2]string{{a, b}, {b, a}}
	for index, pair := range ends {
		local, remote := pair[0], pair[1]
		connectIP := link.Status.Legs[index].ConnectIP
		side := peering.RenderSide(remote, connectIP, chosen.CIDR, cidrs[remote])
		if err := r.applySide(ctx, local, remote, side); err != nil {
			// A peering on one side only is worse than none: the routes point
			// into a link the other router does not hold, so the traffic goes
			// there and dies, where before it would at least have taken the
			// default route.
			r.rollBack(ctx, link)
			r.setPeeringCondition(link, false, "OneSidedRefused",
				fmt.Sprintf("could not write the %s end (%v); the %s end was taken "+
					"back off rather than left as a black hole", local, err, remote))
			link.Status.ObservedGeneration = link.Generation
			return ctrl.Result{RequeueAfter: drainRetry},
				kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before)
		}
		link.Status.Legs[index].Applied = true
	}

	r.setPeeringCondition(link, true, "Established",
		fmt.Sprintf("%s <-> %s over %s (%s <-> %s)",
			a, b, chosen.CIDR, chosen.A, chosen.B))
	r.judgeTraffic(ctx, link, a, b, cidrs)
	link.Status.ObservedGeneration = link.Generation
	return ctrl.Result{}, kube.UpdateStatus(ctx, r.Client, peeringControllerName, link, before)
}

// chooseLink honours a pinned CIDR, otherwise takes the lowest free one.
func (r *ManagedNetworkPeeringReconciler) chooseLink(
	ctx context.Context, link *platformv1alpha1.ManagedNetworkPeering,
) (peering.Link, error) {
	if link.Spec.LinkCIDR != "" {
		return peering.Parse(link.Spec.LinkCIDR)
	}
	// Already chosen: keep it. Re-allocating on every pass would renumber a
	// working link every time something unrelated changed.
	if link.Status.LinkCIDR != "" {
		return peering.Parse(link.Status.LinkCIDR)
	}

	used, err := r.linksInUse(ctx, link.Name)
	if err != nil {
		return peering.Link{}, err
	}
	return peering.Allocate(used)
}

// linksInUse is every point-to-point subnet already held, read from the routers
// themselves and from the other peering objects.
//
// Both, because the two are not the same set during a migration: the endpoint
// still writes peerings this controller has no object for, and an object that
// has chosen a link may not have written it yet.
func (r *ManagedNetworkPeeringReconciler) linksInUse(
	ctx context.Context, exclude string,
) ([]string, error) {
	vpcs := &unstructured.UnstructuredList{}
	vpcs.SetGroupVersionKind(vpcGVK.GroupVersion().WithKind("VpcList"))
	if err := r.List(ctx, vpcs); err != nil {
		return nil, fmt.Errorf("listing VPCs for the link addresses: %w", err)
	}
	var connectIPs []string
	for i := range vpcs.Items {
		entries, _, _ := unstructured.NestedSlice(vpcs.Items[i].Object, "spec", "vpcPeerings")
		for _, raw := range entries {
			entry, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			if address, _ := entry["localConnectIP"].(string); address != "" {
				connectIPs = append(connectIPs, address)
			}
		}
	}
	used := peering.UsedLinks(connectIPs)

	peerings := &platformv1alpha1.ManagedNetworkPeeringList{}
	if err := r.List(ctx, peerings); err != nil {
		return nil, fmt.Errorf("listing peerings for the link addresses: %w", err)
	}
	for i := range peerings.Items {
		if peerings.Items[i].Name == exclude {
			continue
		}
		if cidr := peerings.Items[i].Status.LinkCIDR; cidr != "" {
			used = append(used, cidr)
		}
	}
	sort.Strings(used)
	return used, nil
}

// applySide writes one router's half, re-reading the spec each time.
//
// `spec.vpcPeerings` is a list and a merge patch replaces it wholesale, so two
// peerings touching the same VPC at once each computed their list from the same
// read and the second dropped the first entry — both calls succeeded and half
// the links quietly did not exist. Re-reading and updating with the version the
// read carried turns that into a conflict, which is a retry rather than a loss.
func (r *ManagedNetworkPeeringReconciler) applySide(
	ctx context.Context, local, remote string, side peering.Side,
) error {
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: local}, vpc); err != nil {
		return fmt.Errorf("reading Vpc/%s: %w", local, err)
	}

	spec, _, _ := unstructured.NestedMap(vpc.Object, "spec")
	if spec == nil {
		spec = map[string]any{}
	}
	wanted := map[string]any{
		"vpcPeerings":  withPeering(spec["vpcPeerings"], remote, side.Peering),
		"staticRoutes": withRoutes(spec["staticRoutes"], side.Routes),
		"policyRoutes": withPolicies(spec["policyRoutes"], side.Policies),
	}
	// Written only when it differs. Re-applying an unchanged peering every pass
	// is a write per reconcile on a router shared with other peerings and with
	// the network controller, and each one is a chance for somebody else's
	// concurrent write to lose.
	unchanged := true
	for key, value := range wanted {
		if !equality.Semantic.DeepEqual(spec[key], value) {
			unchanged = false
		}
		spec[key] = value
	}
	if unchanged {
		return nil
	}
	if err := unstructured.SetNestedMap(vpc.Object, spec, "spec"); err != nil {
		return err
	}
	if err := r.Update(ctx, vpc); err != nil {
		return err
	}
	kube.CountWrite(r.Scheme, vpc, peeringControllerName, "updated")
	return nil
}

// samePlan compares the addresses, not the applied flags: an unchanged plan
// must not be rewritten just because one end has since been written.
func samePlan(current, planned []platformv1alpha1.PeeringLeg) bool {
	if len(current) != len(planned) {
		return false
	}
	for i := range planned {
		if current[i].Network != planned[i].Network ||
			current[i].ConnectIP != planned[i].ConnectIP {
			return false
		}
	}
	return true
}

// removeSide takes one router's half back off.
func (r *ManagedNetworkPeeringReconciler) removeSide(ctx context.Context, local, remote string) error {
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: local}, vpc); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("reading Vpc/%s: %w", local, err)
	}
	if !vpc.GetDeletionTimestamp().IsZero() {
		// On its way out anyway, and writing to it would be the resurrection
		// hazard from the other controller.
		return nil
	}

	spec, _, _ := unstructured.NestedMap(vpc.Object, "spec")
	if spec == nil {
		return nil
	}
	remoteCIDRs := routedCIDRs(spec["vpcPeerings"], spec["staticRoutes"], remote)
	spec["vpcPeerings"] = withoutPeering(spec["vpcPeerings"], remote)
	spec["staticRoutes"] = withoutCIDRs(spec["staticRoutes"], "cidr", remoteCIDRs)
	spec["policyRoutes"] = withoutMatches(spec["policyRoutes"], remoteCIDRs)
	if err := unstructured.SetNestedMap(vpc.Object, spec, "spec"); err != nil {
		return err
	}
	if err := r.Update(ctx, vpc); err != nil {
		return err
	}
	kube.CountWrite(r.Scheme, vpc, peeringControllerName, "updated")
	return nil
}

// rollBack removes whatever status says was written.
func (r *ManagedNetworkPeeringReconciler) rollBack(
	ctx context.Context, link *platformv1alpha1.ManagedNetworkPeering,
) {
	// Every planned end, not only the ones known to have landed: a process that
	// stopped mid-write left no report of the second, and removing an end that
	// was never written costs nothing.
	for index := range link.Status.Legs {
		local := link.Status.Legs[index].Network
		remote := link.Status.Legs[1-index].Network
		if err := r.removeSide(ctx, local, remote); err != nil {
			// Reported, not swallowed: a half-peering that could not be undone
			// is the state this whole design is about.
			r.event(link, "Warning", "RollbackFailed",
				fmt.Sprintf("could not take the %s end back off: %v", local, err))
			continue
		}
		link.Status.Legs[index].Applied = false
	}
}

// tearDownPeering removes both ends and lets the object go.
func (r *ManagedNetworkPeeringReconciler) tearDownPeering(
	ctx context.Context, link *platformv1alpha1.ManagedNetworkPeering,
) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(link, peeringFinalizer) {
		return ctrl.Result{}, nil
	}
	// Both ends, from the declaration rather than from status: a peering
	// half-written by a previous process still has to come off entirely, and
	// status may only know about one of them.
	if len(link.Spec.Networks) == 2 {
		a, b := link.Spec.Networks[0], link.Spec.Networks[1]
		for _, pair := range [][2]string{{a, b}, {b, a}} {
			if err := r.removeSide(ctx, pair[0], pair[1]); err != nil {
				return ctrl.Result{}, err
			}
		}
	}
	controllerutil.RemoveFinalizer(link, peeringFinalizer)
	if err := r.Update(ctx, link); err != nil {
		return ctrl.Result{}, fmt.Errorf("releasing the peering: %w", err)
	}
	kube.CountWrite(r.Scheme, link, peeringControllerName, "updated")
	return ctrl.Result{}, nil
}

func (r *ManagedNetworkPeeringReconciler) subnetCIDRs(
	ctx context.Context, vpc string,
) ([]string, error) {
	subnets := &unstructured.UnstructuredList{}
	subnets.SetGroupVersionKind(subnetGVK.GroupVersion().WithKind("SubnetList"))
	if err := r.List(ctx, subnets); err != nil {
		return nil, fmt.Errorf("listing subnets of %s: %w", vpc, err)
	}
	var out []string
	for i := range subnets.Items {
		owner, _, _ := unstructured.NestedString(subnets.Items[i].Object, "spec", "vpc")
		if owner != vpc || !subnets.Items[i].GetDeletionTimestamp().IsZero() {
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

// isolationWillNotLift is the precondition, and the reason the routes go in
// second.
//
// Three outcomes. The traffic is already allowed — nothing is isolated, or the
// prefix is already open — and the routes can go in. The rules are the
// composer's and have not caught up: wait, because it has the declaration and
// the allow is on its way, and writing routes first would create the very
// interval this ordering exists to remove. Or the rules belong to something
// that will never hear about this peering, and the allow is never coming.
//
// Deletion takes the reverse order for free: the finalizer holds the object
// until the routes are off, and the composer only drops the allow once the
// object is gone.
func (r *ManagedNetworkPeeringReconciler) isolationWillNotLift(
	ctx context.Context, a, b string, cidrs map[string][]string,
) (reason, detail string) {
	var stuck, waiting []string
	for _, pair := range [][2]string{{a, b}, {b, a}} {
		local, remote := pair[0], pair[1]
		rules, err := r.aclsOf(ctx, local)
		if err != nil {
			return "Unreadable", err.Error()
		}
		blocked := false
		for _, cidr := range cidrs[remote] {
			source, err := firstAddressOf(cidr)
			if err != nil {
				continue
			}
			if acl.Evaluate(rules, source, "to-lport") != acl.Allowed {
				blocked = true
			}
		}
		if !blocked {
			continue
		}
		owned, err := r.aclsAreComposed(ctx, local)
		if err != nil {
			return "Unreadable", err.Error()
		}
		if owned {
			waiting = append(waiting, local)
			continue
		}
		stuck = append(stuck, local)
	}
	if len(waiting) > 0 && len(stuck) == 0 {
		// The composer has seen the declaration and has not caught up yet.
		// Waiting is the whole point: no route is written until the prefix is
		// open, so there is never an interval where the traffic is routed into
		// a drop.
		return "WaitingForIsolation", fmt.Sprintf(
			"the rules on %s do not let the other side in yet. They are the "+
				"composer's and it has the declaration, so the allow is coming; "+
				"nothing is routed until it does",
			strings.Join(waiting, " and "))
	}
	if len(stuck) == 0 {
		return "", ""
	}
	return "IsolationNotOurs", fmt.Sprintf(
		"the rules on %s drop the other side and that list is not the composer's, "+
			"so the allow is never coming. Nothing was written: routing into a drop "+
			"is worse than not peering — the traffic would go to the link and die "+
			"where it used to take the default route. Describe %s as a "+
			"ManagedNetwork so its rules are composed, or open the prefix by hand",
		strings.Join(stuck, " and "), strings.Join(stuck, " and "))
}

// aclsAreComposed reports whether the composer owns this network's rule lists.
func (r *ManagedNetworkPeeringReconciler) aclsAreComposed(
	ctx context.Context, vpc string,
) (bool, error) {
	subnets := &unstructured.UnstructuredList{}
	subnets.SetGroupVersionKind(subnetGVK.GroupVersion().WithKind("SubnetList"))
	if err := r.List(ctx, subnets); err != nil {
		return false, fmt.Errorf("listing subnets of %s: %w", vpc, err)
	}
	found := false
	for i := range subnets.Items {
		owner, _, _ := unstructured.NestedString(subnets.Items[i].Object, "spec", "vpc")
		if owner != vpc {
			continue
		}
		found = true
		if subnets.Items[i].GetAnnotations()[aclOwnerAnnotation] != aclOwnerOperator {
			return false, nil
		}
	}
	return found, nil
}

// judgeTraffic asks whether a packet would get through, rather than whether
// something wrote an allow.
//
// The routes and the link can be perfect while isolation still drops the peer's
// prefix, and the product has shipped exactly that: peered on both routers,
// reporting Active, carrying nothing in either direction. So each side's rule
// list is evaluated the way OVN evaluates it, against an address out of the
// other side's range.
func (r *ManagedNetworkPeeringReconciler) judgeTraffic(
	ctx context.Context, link *platformv1alpha1.ManagedNetworkPeering,
	a, b string, cidrs map[string][]string,
) {
	var blocked []string
	for _, pair := range [][2]string{{a, b}, {b, a}} {
		local, remote := pair[0], pair[1]
		for _, cidr := range cidrs[remote] {
			source, err := firstAddressOf(cidr)
			if err != nil {
				continue
			}
			rules, err := r.aclsOf(ctx, local)
			if err != nil {
				r.setTrafficCondition(link, false, "Unreadable", err.Error())
				return
			}
			if verdict := acl.Evaluate(rules, source, "to-lport"); verdict != acl.Allowed {
				blocked = append(blocked,
					fmt.Sprintf("%s from %s (%s)", local, cidr, verdict))
			}
		}
	}

	if len(blocked) > 0 {
		r.setTrafficCondition(link, false, "IsolationDrops",
			fmt.Sprintf("the link and the routes are in place and the isolation "+
				"rules still drop the traffic: %s. Whatever owns those rule lists "+
				"has not seen this peering — a ManagedNetwork's composer picks it "+
				"up on its own; a list written elsewhere does not",
				strings.Join(blocked, "; ")))
		return
	}
	r.setTrafficCondition(link, true, "Allowed",
		"both sides let the other's prefixes in")
}

// aclsOf reads the rule lists of every subnet in a network.
func (r *ManagedNetworkPeeringReconciler) aclsOf(
	ctx context.Context, vpc string,
) ([]acl.Rule, error) {
	subnets := &unstructured.UnstructuredList{}
	subnets.SetGroupVersionKind(subnetGVK.GroupVersion().WithKind("SubnetList"))
	if err := r.List(ctx, subnets); err != nil {
		return nil, fmt.Errorf("listing subnets of %s: %w", vpc, err)
	}
	var out []acl.Rule
	for i := range subnets.Items {
		owner, _, _ := unstructured.NestedString(subnets.Items[i].Object, "spec", "vpc")
		if owner != vpc {
			continue
		}
		out = append(out, readACLs(&subnets.Items[i])...)
	}
	return out, nil
}

// firstAddressOf is a representative host inside a prefix.
func firstAddressOf(cidr string) (netip.Addr, error) {
	prefix, err := netip.ParsePrefix(cidr)
	if err != nil {
		return netip.Addr{}, err
	}
	return prefix.Masked().Addr().Next(), nil
}

func (r *ManagedNetworkPeeringReconciler) setTrafficCondition(
	link *platformv1alpha1.ManagedNetworkPeering, ok bool, reason, message string,
) {
	status := metav1.ConditionTrue
	if !ok {
		status = metav1.ConditionFalse
	}
	apimeta.SetStatusCondition(&link.Status.Conditions, metav1.Condition{
		Type:               platformv1alpha1.ConditionTrafficAllowed,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: link.Generation,
	})
}

func (r *ManagedNetworkPeeringReconciler) setAcceptedCondition(
	link *platformv1alpha1.ManagedNetworkPeering, ok bool, reason, message string,
) {
	status := metav1.ConditionTrue
	if !ok {
		status = metav1.ConditionFalse
	}
	apimeta.SetStatusCondition(&link.Status.Conditions, metav1.Condition{
		Type:               platformv1alpha1.ConditionPeeringAccepted,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: link.Generation,
	})
}

func (r *ManagedNetworkPeeringReconciler) setPeeringCondition(
	link *platformv1alpha1.ManagedNetworkPeering, ok bool, reason, message string,
) {
	status := metav1.ConditionTrue
	if !ok {
		status = metav1.ConditionFalse
	}
	apimeta.SetStatusCondition(&link.Status.Conditions, metav1.Condition{
		Type:               platformv1alpha1.ConditionEstablished,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: link.Generation,
	})
}

func (r *ManagedNetworkPeeringReconciler) event(
	link *platformv1alpha1.ManagedNetworkPeering, kind, reason, message string,
) {
	if r.Recorder != nil {
		r.Recorder.Event(link, kind, reason, message)
	}
}

// SetupWithManager wires the controller to the routers it writes and to the
// subnets whose prefixes it routes.
func (r *ManagedNetworkPeeringReconciler) SetupWithManager(mgr ctrl.Manager) error {
	toPeerings := handler.EnqueueRequestsFromMapFunc(
		func(ctx context.Context, _ client.Object) []reconcile.Request {
			list := &platformv1alpha1.ManagedNetworkPeeringList{}
			if err := r.List(ctx, list); err != nil {
				return nil
			}
			out := make([]reconcile.Request, 0, len(list.Items))
			for i := range list.Items {
				out = append(out, reconcile.Request{
					NamespacedName: types.NamespacedName{Name: list.Items[i].Name},
				})
			}
			return out
		})

	subnets := &unstructured.Unstructured{}
	subnets.SetGroupVersionKind(subnetGVK)

	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.ManagedNetworkPeering{}).
		Watches(subnets, toPeerings).
		Named(peeringControllerName).
		Complete(r)
}

// --- list surgery, kept together so the shapes are in one place -------------

func withPeering(existing any, remote string, entry map[string]any) []any {
	out := withoutPeering(existing, remote)
	return append(out, entry)
}

func withoutPeering(existing any, remote string) []any {
	items, _ := existing.([]any)
	out := make([]any, 0, len(items))
	for _, raw := range items {
		item, ok := raw.(map[string]any)
		if ok {
			if name, _ := item["remoteVpc"].(string); name == remote {
				continue
			}
		}
		out = append(out, raw)
	}
	return out
}

func withRoutes(existing any, routes []map[string]any) []any {
	wanted := map[string]bool{}
	for _, route := range routes {
		cidr, _ := route["cidr"].(string)
		wanted[cidr] = true
	}
	out := withoutCIDRsSet(existing, "cidr", wanted)
	for _, route := range routes {
		out = append(out, route)
	}
	return out
}

func withPolicies(existing any, policies []map[string]any) []any {
	wanted := map[string]bool{}
	for _, policy := range policies {
		match, _ := policy["match"].(string)
		wanted[match] = true
	}
	items, _ := existing.([]any)
	out := make([]any, 0, len(items))
	for _, raw := range items {
		item, ok := raw.(map[string]any)
		if ok {
			if match, _ := item["match"].(string); wanted[match] {
				continue
			}
		}
		out = append(out, raw)
	}
	for _, policy := range policies {
		out = append(out, policy)
	}
	return out
}

// routedCIDRs is what this peer's routes point at, read off the router rather
// than recomputed: by the time a peering is removed the other network's subnets
// may already be gone, and a route nobody can name is a route nobody removes.
func routedCIDRs(peerings, routes any, remote string) map[string]bool {
	entries, _ := peerings.([]any)
	var connectIP string
	for _, raw := range entries {
		entry, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if name, _ := entry["remoteVpc"].(string); name != remote {
			continue
		}
		connectIP, _ = entry["localConnectIP"].(string)
	}
	if connectIP == "" {
		return nil
	}
	link, err := peering.Parse(connectIP)
	if err != nil {
		return nil
	}
	far := link.A
	if strings.HasPrefix(connectIP, link.A+"/") {
		far = link.B
	}

	out := map[string]bool{}
	items, _ := routes.([]any)
	for _, raw := range items {
		route, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if hop, _ := route["nextHopIP"].(string); hop != far {
			continue
		}
		if cidr, _ := route["cidr"].(string); cidr != "" {
			out[cidr] = true
		}
	}
	return out
}

func withoutCIDRs(existing any, field string, cidrs map[string]bool) []any {
	return withoutCIDRsSet(existing, field, cidrs)
}

func withoutCIDRsSet(existing any, field string, cidrs map[string]bool) []any {
	items, _ := existing.([]any)
	out := make([]any, 0, len(items))
	for _, raw := range items {
		item, ok := raw.(map[string]any)
		if ok {
			if value, _ := item[field].(string); cidrs[value] {
				continue
			}
		}
		out = append(out, raw)
	}
	return out
}

func withoutMatches(existing any, cidrs map[string]bool) []any {
	matches := map[string]bool{}
	for cidr := range cidrs {
		matches["ip4.dst == "+cidr] = true
	}
	items, _ := existing.([]any)
	out := make([]any, 0, len(items))
	for _, raw := range items {
		item, ok := raw.(map[string]any)
		if ok {
			if match, _ := item["match"].(string); matches[match] {
				continue
			}
		}
		out = append(out, raw)
	}
	return out
}
