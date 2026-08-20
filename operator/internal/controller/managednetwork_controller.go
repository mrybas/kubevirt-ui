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
	"strings"

	appsv1 "k8s.io/api/apps/v1"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
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

	if err := r.ensureVPC(ctx, net, nextHop); err != nil {
		r.setNetworkCondition(net, platformv1alpha1.ConditionNetworkReady, false, "WriteFailed", err.Error())
		net.Status.ObservedGeneration = net.Generation
		_ = kube.UpdateStatus(ctx, r.Client, networkControllerName, net, before)
		return ctrl.Result{}, err
	}
	if err := r.ensureSubnet(ctx, net, gateway, r.resolveDNSServer(ctx, net, r.kubeOVNNamespaceFor(ctx))); err != nil {
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
) error {
	want := network.VPCSpec(net)
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(vpcGVK)
	live.SetName(net.Name)

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

		if wanted := network.Attachments(net); len(wanted) > 0 {
			liveAttached, _, _ := unstructured.NestedStringSlice(spec, "extraExternalSubnets")
			if merged, changed := network.MergeStrings(liveAttached, wanted); changed {
				spec["extraExternalSubnets"] = toAnySlice(merged)
			}
		}
		return unstructured.SetNestedMap(live.Object, spec, "spec")
	})
	if err != nil {
		return fmt.Errorf("Vpc/%s: %w", net.Name, err)
	}
	return nil
}

// ensureSubnet writes the default subnet.
func (r *ManagedNetworkReconciler) ensureSubnet(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork, gateway, dnsServer string,
) error {
	name := network.DefaultSubnetName(net)
	want := network.SubnetSpec(net, gateway, dnsServer)
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(subnetGVK)
	live.SetName(name)

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
		// `acls` is deliberately absent from `want` and never touched here: the
		// isolation reconciler is its single writer until the composer adopts
		// it with a diff-empty handover.
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

func toAnySlice(in []string) []any {
	out := make([]any, 0, len(in))
	for _, v := range in {
		out = append(out, v)
	}
	return out
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
