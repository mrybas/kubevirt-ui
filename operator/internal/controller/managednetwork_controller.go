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
	"errors"
	"fmt"
	"os"
	"strings"

	appsv1 "k8s.io/api/apps/v1"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/acl"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/network"
)

const networkControllerName = "managednetwork"

// ManagedNetworkReconciler keeps a kube-ovn VPC and its default subnet in line
// with one declaration.
//
// It writes the two objects and their attachment to the external plane. It
// deliberately does not write `Subnet.spec.acls`: that list has one writer
// today — the isolation reconciler in the UI backend — and taking it over needs
// an adoption step that can prove the rendered list equals the live one first.
// Two writers of one ACL list is the failure this operator exists to remove,
// and starting by reproducing it would be an odd way to begin.
type ManagedNetworkReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder

	// APIReader reads straight from the API server. Used for the one lookup
	// that must not build a cache: the cluster's service network, which is
	// asked for once per process and lives among every Pod and ConfigMap in
	// kube-system.
	APIReader client.Reader

	// KubeOVNNamespace is where kube-ovn runs. Empty means find it.
	KubeOVNNamespace string

	// TransitSubnet is the control-plane leg. Named here only so it can be
	// refused: a tenant's workers reach their control plane over it, and the
	// tenant controller attaches it. Withdrawing it from under a live tenant
	// would be two writers with opposite intentions flapping the one leg that
	// must not flap.
	TransitSubnet string

	// TenantSupernet is the aggregate every tenant network is carved from, and
	// what the isolation floor is scoped to. Empty means no isolation is
	// written at all — a drop with nothing to scope it to would take the
	// internet with it.
	TenantSupernet string

	// MgmtCIDRs is where the management plane is. Empty means each node's own
	// address as a /32, which is exact and cannot over-block.
	MgmtCIDRs []string

	// ServiceCIDR states the cluster's service network for installs where it
	// cannot be discovered — a managed control plane exposes neither the
	// kubeadm ConfigMap nor an apiserver pod. Set once on the operator instead
	// of on every network.
	ServiceCIDR string

	serviceCIDRs serviceCIDRCache
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managednetworks,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managednetworks/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeovn.io,resources=vpcs;vpc-dnses,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;patch
// +kubebuilder:rbac:groups="",resources=configmaps;pods,verbs=get;list;watch

// Reconcile writes the VPC and its default subnet.
func (r *ManagedNetworkReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	net := &platformv1alpha1.ManagedNetwork{}
	if err := r.Get(ctx, req.NamespacedName, net); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !net.DeletionTimestamp.IsZero() {
		return r.tearDown(ctx, net)
	}
	if net.Annotations[pausedAnnotation] == "true" {
		return ctrl.Result{}, nil
	}

	// Done before anything is built, so a network that asked to be cascaded
	// cannot exist for even one pass without the finalizer that makes the
	// cascade possible.
	if updated, err := r.reconcileFinalizer(ctx, net); err != nil {
		return ctrl.Result{}, err
	} else if updated {
		// The object just changed under us; come back with the fresh copy
		// rather than writing status against a stale resourceVersion.
		return ctrl.Result{Requeue: true}, nil
	}

	before := net.DeepCopy()

	gateway, err := network.Gateway(net)
	if err != nil {
		r.setNetworkCondition(net, platformv1alpha1.ConditionNetworkReady, false, "BadCIDR", err.Error())
		net.Status.ObservedGeneration = net.Generation
		return ctrl.Result{}, kube.UpdateStatus(ctx, r.Client, networkControllerName, net, before)
	}
	net.Status.Gateway = gateway
	net.Status.SubnetName = network.DefaultSubnetName(net)

	// The next hop is read from the egress subnet, not configured. Resolving it
	// before anything is written means a VPC never gets the attachment without
	// the route that makes it useful.
	nextHop, attachErr := r.egressNextHop(ctx, net)
	if attachErr != nil {
		return ctrl.Result{}, attachErr
	}
	net.Status.DefaultRouteVia = nextHop
	net.Status.Attachments = network.Attachments(net)

	// What was applied on the last pass, read before this one overwrites it:
	// it is the record of which legs and which route are this operator's to
	// take back when they stop being declared.
	if err := r.ensureVPC(ctx, net, nextHop,
		before.Status.Attachments, before.Status.DefaultRouteVia); err != nil {
		if errors.Is(err, errObjectGoing) {
			return r.standDown(ctx, net, before, "Vpc/"+net.Name)
		}
		r.setNetworkCondition(net, platformv1alpha1.ConditionNetworkReady, false, "WriteFailed", err.Error())
		net.Status.ObservedGeneration = net.Generation
		_ = kube.UpdateStatus(ctx, r.Client, networkControllerName, net, before)
		return ctrl.Result{}, err
	}
	// Composed before the subnet is written, not after. A subnet created open
	// and closed a moment later is open for that moment: kube-ovn realises it
	// as soon as it exists, and "eventually consistent" is not a property you
	// want on the boundary between two tenants. So the rules go into the create
	// payload itself, the way the path this replaces always did.
	initialACLs, _ := r.composeACLs(ctx, net)
	if err := r.ensureSubnet(
		ctx, net, gateway,
		r.resolveDNSServer(ctx, net, r.kubeOVNNamespaceFor(ctx)), initialACLs,
	); err != nil {
		if errors.Is(err, errObjectGoing) {
			return r.standDown(ctx, net, before,
				"Subnet/"+network.DefaultSubnetName(net))
		}
		r.setNetworkCondition(net, platformv1alpha1.ConditionNetworkReady, false, "WriteFailed", err.Error())
		net.Status.ObservedGeneration = net.Generation
		_ = kube.UpdateStatus(ctx, r.Client, networkControllerName, net, before)
		return ctrl.Result{}, err
	}

	kubeOVNNS := r.kubeOVNNamespaceFor(ctx)
	net.Status.DNSServer = r.resolveDNSServer(ctx, net, kubeOVNNS)
	if err := r.reconcileDNS(ctx, net, kubeOVNNS); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.reconcileACLs(ctx, net); err != nil {
		return ctrl.Result{}, err
	}

	r.judgeAttachment(ctx, net, nextHop)

	// Ready is not "the objects exist". A network this controller built that
	// asked to be closed and is not closed yet is not ready to be used, and
	// saying otherwise is how a tenant network ends up carrying workloads while
	// reachable from every other tenant.
	//
	// Only for networks it built, though. A network merely described here has
	// its rule list written by something else, and this controller is in no
	// position to call that a failure — it reports what it sees on the Isolated
	// condition and leaves Ready to mean what it can actually answer for.
	if isolation := apimeta.FindStatusCondition(
		net.Status.Conditions, platformv1alpha1.ConditionIsolated,
	); cascadeOnDelete(net) && network.IsIsolated(net) && net.Spec.Role == "" &&
		(isolation == nil || isolation.Status != metav1.ConditionTrue) {
		reason, message := "IsolationPending", "the isolation rules are not on the subnet yet"
		if isolation != nil {
			reason, message = isolation.Reason, isolation.Message
		}
		r.setNetworkCondition(net, platformv1alpha1.ConditionNetworkReady, false, reason, message)
		net.Status.ObservedGeneration = net.Generation
		return ctrl.Result{RequeueAfter: drainRetry},
			kube.UpdateStatus(ctx, r.Client, networkControllerName, net, before)
	}

	r.setNetworkCondition(net, platformv1alpha1.ConditionNetworkReady, true, "Built",
		fmt.Sprintf("Vpc/%s and Subnet/%s exist with %s",
			net.Name, network.DefaultSubnetName(net), net.Spec.CIDR))

	net.Status.ObservedGeneration = net.Generation
	return ctrl.Result{}, kube.UpdateStatus(ctx, r.Client, networkControllerName, net, before)
}

// egressNextHop reads the gateway of the subnet the default route leaves
// through.
//
// It is a fact of the cluster, so it is read from the cluster. Carrying it as
// configuration alongside the subnet that already states it is the same number
// in two places, which is the same number right up until one of them changes.
func (r *ManagedNetworkReconciler) egressNextHop(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) (string, error) {
	plane := net.Spec.ExternalPlane
	if plane == nil || plane.EgressSubnet == "" {
		return "", nil
	}
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: plane.EgressSubnet}, subnet); err != nil {
		if apierrors.IsNotFound(err) {
			return "", nil
		}
		return "", fmt.Errorf("reading the egress subnet %s: %w", plane.EgressSubnet, err)
	}
	hop, _, _ := unstructured.NestedString(subnet.Object, "spec", "gateway")
	return hop, nil
}

// ensureVPC writes the router.
func (r *ManagedNetworkReconciler) ensureVPC(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork, nextHop string,
	appliedAttachments []string, appliedNextHop string,
) error {
	// A leg a live tenant is using is not this object's to take back, however
	// the declaration reads. The tenant controller attaches the control-plane
	// leg because its workers reach their control plane over it; withdrawing it
	// here would give one leg two writers with opposite intentions, and the
	// flap would land on the path that must not flap.
	held, heldErr := r.legsHeldByTenants(ctx, net)
	if heldErr != nil {
		return heldErr
	}
	want := network.VPCSpec(net)
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(vpcGVK)
	live.SetName(net.Name)

	if going, err := r.beingDeleted(ctx, vpcGVK, net.Name); err != nil {
		return err
	} else if going {
		return errObjectGoing
	}

	_, err := kube.Ensure(ctx, r.Client, networkControllerName, live, func() error {
		mergeLabels(live, network.Labels(net))
		spec, _, _ := unstructured.NestedMap(live.Object, "spec")
		if spec == nil {
			spec = map[string]any{}
		}
		// Only the keys this controller renders. kube-ovn writes its own
		// defaults back into the specs of its own objects, and replacing the
		// map wholesale would have the two rewriting each other forever.
		for k, v := range want {
			spec[k] = v
		}

		// The two list-valued fields are merged rather than set: VPC peering
		// writes staticRoutes and the egress-gateway attach path appends to
		// extraExternalSubnets, so replacing either would delete another
		// writer's work on the first pass. Merging also means adopting a
		// network the product already built writes nothing at all — kube-ovn
		// stores three defaulted keys per route that nothing here sets, and an
		// exact comparison would rewrite the list forever without moving
		// resourceVersion.
		liveRoutes, _, _ := unstructured.NestedSlice(spec, "staticRoutes")
		if merged, changed := network.MergeRoutes(
			liveRoutes, network.DesiredRoutes(net, nextHop)); changed {
			spec["staticRoutes"] = merged
		}

		wanted := network.Attachments(net)
		liveAttached, _, _ := unstructured.NestedStringSlice(spec, "extraExternalSubnets")
		if len(wanted) > 0 {
			if merged, changed := network.MergeStrings(liveAttached, wanted); changed {
				liveAttached = merged
				spec["extraExternalSubnets"] = toAnySlice(merged)
			}
		}
		// And the other direction, which was missing: a leg this operator
		// attached and no longer declares is taken back. Only ours — the record
		// of the last pass is what says which those are, because "live minus
		// wanted" would delete another writer's work, and the merge above
		// exists precisely to protect it.
		if kept, changed := network.Withdraw(
			liveAttached, append(wanted, held...), appliedAttachments); changed {
			spec["extraExternalSubnets"] = toAnySlice(kept)
		}
		if nextHop == "" {
			// The default route goes with the leg it left through, matched on
			// the hop it was written with so somebody else's default route
			// through another gateway stays where it is.
			routes, _, _ := unstructured.NestedSlice(spec, "staticRoutes")
			if kept, changed := network.WithdrawRoute(routes, appliedNextHop); changed {
				spec["staticRoutes"] = kept
			}
		}
		return unstructured.SetNestedMap(live.Object, spec, "spec")
	})
	if err != nil {
		return fmt.Errorf("Vpc/%s: %w", net.Name, err)
	}
	return nil
}

// legsHeldByTenants is the attachments this network must keep because somebody
// is living behind them.
//
// Only the control-plane leg, and only while a tenant declares this network:
// an egress leg can be taken away from a tenant — that is a deliberate loss of
// internet, which is the whole point of the plane being separate — but the
// control-plane leg cannot, because losing it is losing the cluster.
func (r *ManagedNetworkReconciler) legsHeldByTenants(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) ([]string, error) {
	transit := r.TransitSubnet
	if transit == "" {
		transit = os.Getenv("TENANTS_CP_TRANSIT_SUBNET")
	}
	if transit == "" {
		return nil, nil
	}
	tenants := &platformv1alpha1.ManagedTenantList{}
	if err := r.List(ctx, tenants); err != nil {
		if apimeta.IsNoMatchError(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("reading the tenants of %s: %w", net.Name, err)
	}
	for i := range tenants.Items {
		if tenants.Items[i].Spec.Network == net.Name {
			return []string{transit}, nil
		}
	}
	return nil, nil
}

// ensureSubnet writes the default subnet.
func (r *ManagedNetworkReconciler) ensureSubnet(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
	gateway, dnsServer string, initialACLs []acl.Rule,
) error {
	name := network.DefaultSubnetName(net)
	want := network.SubnetSpec(net, gateway, dnsServer)
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(subnetGVK)
	live.SetName(name)

	if going, err := r.beingDeleted(ctx, subnetGVK, name); err != nil {
		return err
	} else if going {
		return errObjectGoing
	}

	_, err := kube.Ensure(ctx, r.Client, networkControllerName, live, func() error {
		mergeLabels(live, network.Labels(net))

		// The opt-out annotation is written only when the answer is "no", and
		// removed when it changes back — a stale opt-out is a network that
		// silently stopped being isolated.
		//
		// Touched only when it actually differs. Handing SetAnnotations an
		// empty map where the object had none is a change as far as the diff
		// check is concerned, and the API server stores nil again — so an
		// isolated network would be updated on every single pass. Measured:
		// one extra write per reconcile, with nothing in the object to show it.
		annotations := live.GetAnnotations()
		_, present := annotations[network.IsolationOptOutAnnotation]
		switch {
		case network.IsIsolated(net) && present:
			delete(annotations, network.IsolationOptOutAnnotation)
			live.SetAnnotations(annotations)
		case !network.IsIsolated(net) &&
			annotations[network.IsolationOptOutAnnotation] != network.IsolationOptOutValue:
			if annotations == nil {
				annotations = map[string]string{}
			}
			annotations[network.IsolationOptOutAnnotation] = network.IsolationOptOutValue
			live.SetAnnotations(annotations)
		}

		spec, _, _ := unstructured.NestedMap(live.Object, "spec")
		if spec == nil {
			spec = map[string]any{}
		}
		for k, v := range want {
			spec[k] = v
		}
		// On create only, and only for a network this controller owns: the
		// rules ship with the object so it is never briefly open. An existing
		// subnet is left alone here — its list belongs to whoever writes it
		// until the composer can prove a handover changes nothing.
		if live.GetResourceVersion() == "" && cascadeOnDelete(net) && len(initialACLs) > 0 {
			if err := unstructured.SetNestedMap(live.Object, spec, "spec"); err != nil {
				return err
			}
			if err := writeACLs(live, initialACLs); err != nil {
				return err
			}
			annotations := live.GetAnnotations()
			if annotations == nil {
				annotations = map[string]string{}
			}
			annotations[aclOwnerAnnotation] = aclOwnerOperator
			live.SetAnnotations(annotations)
			return nil
		}
		return unstructured.SetNestedMap(live.Object, spec, "spec")
	})
	if err != nil {
		return fmt.Errorf("Subnet/%s: %w", name, err)
	}
	return nil
}

// judgeAttachment reports whether the VPC really is on the external plane.
//
// Read back from the object rather than assumed from having written it: the
// interesting failure here is a VPC that carries the array, reports healthy,
// and has no port.
func (r *ManagedNetworkReconciler) judgeAttachment(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork, nextHop string,
) {
	wanted := network.Attachments(net)
	if len(wanted) == 0 {
		r.setNetworkCondition(net, platformv1alpha1.ConditionAttached, true, "NotRequested",
			"no external plane asked for; this network does not leave itself")
		return
	}

	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(vpcGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: net.Name}, live); err != nil {
		r.setNetworkCondition(net, platformv1alpha1.ConditionAttached, false, "Unreadable", err.Error())
		return
	}
	enabled, _, _ := unstructured.NestedBool(live.Object, "spec", "enableExternal")
	got, _, _ := unstructured.NestedStringSlice(live.Object, "spec", "extraExternalSubnets")

	var missing []string
	for _, want := range wanted {
		found := false
		for _, have := range got {
			if have == want {
				found = true
				break
			}
		}
		if !found {
			missing = append(missing, want)
		}
	}
	switch {
	case !enabled:
		r.setNetworkCondition(net, platformv1alpha1.ConditionAttached, false, "MasterSwitchOff",
			"extraExternalSubnets is set and enableExternal is not; kube-ovn does "+
				"not read the array without it and the VPC has no external port")
	case len(missing) > 0:
		r.setNetworkCondition(net, platformv1alpha1.ConditionAttached, false, "NotAttached",
			"missing from the VPC: "+strings.Join(missing, ", "))
	case net.Spec.ExternalPlane.EgressSubnet != "" && nextHop == "":
		r.setNetworkCondition(net, platformv1alpha1.ConditionAttached, false, "NoNextHop",
			fmt.Sprintf("Subnet/%s has no gateway, so there is no default route to "+
				"write; the VPC is attached and cannot leave",
				net.Spec.ExternalPlane.EgressSubnet))
	default:
		message := "attached to " + strings.Join(wanted, ", ")
		if nextHop != "" {
			message += "; default route via " + nextHop
		} else {
			message += "; no default route, by declaration"
		}
		r.setNetworkCondition(net, platformv1alpha1.ConditionAttached, true, "Attached", message)
	}
}

// standDown stops writing and says why. Something else is taking this network
// apart, and the only useful thing to do is get out of its way.
func (r *ManagedNetworkReconciler) standDown(
	ctx context.Context, net, before *platformv1alpha1.ManagedNetwork, what string,
) (ctrl.Result, error) {
	r.setNetworkCondition(net, platformv1alpha1.ConditionNetworkReady, false, "BeingDeleted",
		what+" is being deleted by something else; this controller has stopped "+
			"writing to it. Remove this object if the network is going away.")
	net.Status.ObservedGeneration = net.Generation
	return ctrl.Result{RequeueAfter: drainRetry},
		kube.UpdateStatus(ctx, r.Client, networkControllerName, net, before)
}

// kubeOVNNamespaceFor is where kube-ovn runs: configured, or found from its own
// CNI DaemonSet.
func (r *ManagedNetworkReconciler) kubeOVNNamespaceFor(ctx context.Context) string {
	if r.KubeOVNNamespace != "" {
		return r.KubeOVNNamespace
	}
	list := &appsv1.DaemonSetList{}
	if err := r.List(ctx, list); err != nil {
		return ""
	}
	for i := range list.Items {
		if list.Items[i].Name == "kube-ovn-cni" {
			return list.Items[i].Namespace
		}
	}
	return ""
}

// errObjectGoing means the thing this controller was about to write is on its
// way out, put there by something else.
//
// Writing to an object with a deletionTimestamp is legal and is exactly the
// wrong thing to do here: `CreateOrUpdate` on a subnet that has just been
// deleted either keeps a dying object alive or recreates one the deleter
// believes is gone. Two reconcilers pulling in opposite directions is how a
// teardown wedges, and the half of it this controller owns is not writing.
var errObjectGoing = errors.New("the object is being deleted by something else")

// beingDeleted reports whether the live object is on its way out.
func (r *ManagedNetworkReconciler) beingDeleted(
	ctx context.Context, gvk schema.GroupVersionKind, name string,
) (bool, error) {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(gvk)
	if err := r.Get(ctx, types.NamespacedName{Name: name}, obj); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, fmt.Errorf("reading %s/%s: %w", gvk.Kind, name, err)
	}
	return !obj.GetDeletionTimestamp().IsZero(), nil
}

func toAnySlice(in []string) []any {
	out := make([]any, 0, len(in))
	for _, v := range in {
		out = append(out, v)
	}
	return out
}

// mergeAnnotations is mergeLabels for annotations, and it exists because
// replacing the map cost a live tenant two of them.
//
// Adopting `uat-t1` stripped `kubevirt-ui.io/worker-type` and
// `kubevirt-ui.io/enable-oidc` from its Cluster — written by the product,
// carried by every tenant beside it, and gone the moment this operator wrote
// the object. Metadata somebody else put there is not this writer's to clear
// just because it does not render it.
func mergeAnnotations(obj *unstructured.Unstructured, want map[string]string) {
	annotations := obj.GetAnnotations()
	if annotations == nil {
		annotations = map[string]string{}
	}
	for k, v := range want {
		annotations[k] = v
	}
	obj.SetAnnotations(annotations)
}

func mergeLabels(obj *unstructured.Unstructured, want map[string]string) {
	labels := obj.GetLabels()
	if labels == nil {
		labels = map[string]string{}
	}
	for k, v := range want {
		labels[k] = v
	}
	obj.SetLabels(labels)
}

func (r *ManagedNetworkReconciler) setNetworkCondition(
	net *platformv1alpha1.ManagedNetwork, kind string, ok bool, reason, message string,
) {
	status := metav1.ConditionTrue
	if !ok {
		status = metav1.ConditionFalse
	}
	apimeta.SetStatusCondition(&net.Status.Conditions, metav1.Condition{
		Type:               kind,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: net.Generation,
	})
}

// SetupWithManager wires the controller to the objects it owns and to the
// subnet it reads the next hop from.
func (r *ManagedNetworkReconciler) SetupWithManager(mgr ctrl.Manager) error {
	toNetworks := handler.EnqueueRequestsFromMapFunc(
		func(ctx context.Context, _ client.Object) []reconcile.Request {
			list := &platformv1alpha1.ManagedNetworkList{}
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

	vpcs := &unstructured.Unstructured{}
	vpcs.SetGroupVersionKind(vpcGVK)
	subnets := &unstructured.Unstructured{}
	subnets.SetGroupVersionKind(subnetGVK)
	vpcDNSes := &unstructured.Unstructured{}
	vpcDNSes.SetGroupVersionKind(vpcDNSGVK)

	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.ManagedNetwork{}).
		Watches(vpcs, toNetworks).
		Watches(subnets, toNetworks).
		Watches(vpcDNSes, toNetworks).
		// A declared peering opens the prefix before anything is routed, which
		// is what keeps the routes from ever pointing into a drop.
		Watches(&platformv1alpha1.ManagedNetworkPeering{}, toNetworks).
		// The whole point of moving the service route here: kube-ovn creates
		// the VpcDns Deployment after the object, so the route used to be
		// applied best-effort at create time and then only by a person calling
		// an endpoint. A Deployment write now wakes the controller.
		Watches(&appsv1.Deployment{}, toNetworks,
			builder.WithPredicates(predicate.NewPredicateFuncs(func(o client.Object) bool {
				return strings.HasPrefix(o.GetName(), "vpc-dns-")
			}))).
		Named(networkControllerName).
		Complete(r)
}
