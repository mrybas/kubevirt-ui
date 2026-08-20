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
	"k8s.io/apimachinery/pkg/api/equality"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/announce"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

const (
	announceControllerName = "announcementpolicy"

	// frrConfigName is the object frr-k8s reads. One per cluster, owned by the
	// policy, and the single writer of it — two writers of one BGP
	// configuration is an outage with extra steps.
	frrConfigName = "kubevirt-ui-b3"
)

var (
	ovnEipGVK = schema.GroupVersionKind{Group: "kubeovn.io", Version: "v1", Kind: "OvnEip"}
	frrConfigGVK = schema.GroupVersionKind{
		Group: "frrk8s.metallb.io", Version: "v1beta1", Kind: "FRRConfiguration",
	}
	frrNodeStateGVK = schema.GroupVersionKind{
		Group: "frrk8s.metallb.io", Version: "v1beta1", Kind: "FRRNodeState",
	}
)

// AnnouncementPolicyReconciler keeps the border's view of tenant networks in
// line with the cluster's actual routing.
//
// It replaces a pass over the same logic every thirty seconds from inside the
// UI backend. The pass itself was correct; running it on a timer inside a
// request-serving process was not — nothing woke it when a network appeared,
// and nothing recorded whether FRR had accepted what it produced.
type AnnouncementPolicyReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=announcementpolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=announcementpolicies/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeovn.io,resources=ovn-eips,verbs=get;list;watch
// +kubebuilder:rbac:groups=frrk8s.metallb.io,resources=frrconfigurations,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=frrk8s.metallb.io,resources=frrnodestates,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=nodes,verbs=get;list;watch

// Reconcile renders the announcements and reports what FRR did with them.
func (r *AnnouncementPolicyReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	policy := &platformv1alpha1.AnnouncementPolicy{}
	if err := r.Get(ctx, req.NamespacedName, policy); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if policy.Annotations[pausedAnnotation] == "true" || !policy.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	before := policy.DeepCopy()

	announcements, err := r.collect(ctx, policy)
	if err != nil {
		return ctrl.Result{}, err
	}
	nodes, err := r.announceNodes(ctx, policy)
	if err != nil {
		return ctrl.Result{}, err
	}
	if len(nodes) == 0 {
		r.setAccepted(policy, false, "NoNodes",
			"no Ready worker to announce from; the border would hear nothing")
		policy.Status.ObservedGeneration = policy.Generation
		return ctrl.Result{}, kube.UpdateStatus(ctx, r.Client, announceControllerName, policy, before)
	}

	if err := r.writeConfiguration(ctx, policy, announcements, nodes); err != nil {
		return ctrl.Result{}, err
	}

	failures, err := r.reloadFailures(ctx, nodes)
	if err != nil {
		return ctrl.Result{}, err
	}

	policy.Status.Announced = toAnnouncedPrefixes(announcements)
	policy.Status.Nodes = nodes
	policy.Status.ReloadFailures = failures
	policy.Status.ObservedGeneration = policy.Generation
	if len(failures) == 0 {
		r.setAccepted(policy, true, "Reloaded",
			fmt.Sprintf("%d prefix(es) advertised from %s",
				len(announcements), strings.Join(nodes, ", ")))
	} else {
		// FRR keeps the previous configuration when a reload fails, so what is
		// already advertised survives and what was just added silently is not.
		// Nothing else in the cluster says so.
		names := make([]string, 0, len(failures))
		for _, f := range failures {
			names = append(names, f.Node)
		}
		r.setAccepted(policy, false, "ReloadRejected",
			fmt.Sprintf("FRR rejected the configuration on %s; anything newly attached "+
				"is not being advertised", strings.Join(names, ", ")))
	}

	return ctrl.Result{}, kube.UpdateStatus(ctx, r.Client, announceControllerName, policy, before)
}

// collect works out which networks may be advertised and where to send them.
func (r *AnnouncementPolicyReconciler) collect(
	ctx context.Context, policy *platformv1alpha1.AnnouncementPolicy,
) ([]announce.Announcement, error) {
	externalName := policy.Spec.ExternalSubnet
	if externalName == "" {
		externalName = "external"
	}

	subnets := &unstructured.UnstructuredList{}
	subnets.SetGroupVersionKind(subnetGVK.GroupVersion().WithKind("SubnetList"))
	if err := r.List(ctx, subnets); err != nil {
		return nil, fmt.Errorf("listing subnets: %w", err)
	}

	externalCIDR := ""
	for i := range subnets.Items {
		if subnets.Items[i].GetName() == externalName {
			externalCIDR, _, _ = unstructured.NestedString(subnets.Items[i].Object, "spec", "cidrBlock")
		}
	}
	if externalCIDR == "" {
		// Without it there is nothing to compare a default route against, and
		// announcing on a guess would put wrong paths on the border.
		return nil, nil
	}

	routed, err := r.routedVPCs(ctx, externalCIDR)
	if err != nil {
		return nil, err
	}

	legs, err := r.routerLegs(ctx, externalName, routed)
	if err != nil {
		return nil, err
	}

	var out []announce.Announcement
	for i := range subnets.Items {
		spec := subnets.Items[i].Object
		vpc, _, _ := unstructured.NestedString(spec, "spec", "vpc")
		cidr, _, _ := unstructured.NestedString(spec, "spec", "cidrBlock")
		if hop, ok := legs[vpc]; ok && cidr != "" {
			out = append(out, announce.Announcement{VPC: vpc, CIDR: cidr, NextHop: hop})
		}
	}
	return out, nil
}

// routedVPCs are the ones whose default route leaves through the external plane.
func (r *AnnouncementPolicyReconciler) routedVPCs(
	ctx context.Context, externalCIDR string,
) (map[string]struct{}, error) {
	vpcs := &unstructured.UnstructuredList{}
	vpcs.SetGroupVersionKind(vpcGVK.GroupVersion().WithKind("VpcList"))
	if err := r.List(ctx, vpcs); err != nil {
		return nil, fmt.Errorf("listing VPCs: %w", err)
	}

	out := map[string]struct{}{}
	for i := range vpcs.Items {
		routes, _, _ := unstructured.NestedSlice(vpcs.Items[i].Object, "spec", "staticRoutes")
		var hops []string
		for _, raw := range routes {
			route, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			if cidr, _ := route["cidr"].(string); cidr != "0.0.0.0/0" {
				continue
			}
			if hop, _ := route["nextHopIP"].(string); hop != "" {
				hops = append(hops, hop)
			}
		}
		if announce.RoutedVia(hops, externalCIDR) {
			out[vpcs.Items[i].GetName()] = struct{}{}
		}
	}
	return out, nil
}

// routerLegs is each routed VPC's address on the external plane — the next hop
// the border is told to use.
func (r *AnnouncementPolicyReconciler) routerLegs(
	ctx context.Context, externalName string, routed map[string]struct{},
) (map[string]string, error) {
	eips := &unstructured.UnstructuredList{}
	eips.SetGroupVersionKind(ovnEipGVK.GroupVersion().WithKind("OvnEipList"))
	if err := r.List(ctx, eips); err != nil {
		return nil, fmt.Errorf("listing router legs: %w", err)
	}

	legs := map[string]string{}
	suffix := "-" + externalName
	for i := range eips.Items {
		obj := eips.Items[i].Object
		if kind, _, _ := unstructured.NestedString(obj, "spec", "type"); kind != "lrp" {
			continue
		}
		if sub, _, _ := unstructured.NestedString(obj, "spec", "externalSubnet"); sub != externalName {
			continue
		}
		address, _, _ := unstructured.NestedString(obj, "status", "v4Ip")
		name := eips.Items[i].GetName()
		if address == "" || !strings.HasSuffix(name, suffix) {
			continue
		}
		// The object carries no VPC field for a router port; the name is
		// `<vpc>-<external subnet>` by construction.
		vpc := strings.TrimSuffix(name, suffix)
		if _, ok := routed[vpc]; ok {
			legs[vpc] = address
		}
	}
	return legs, nil
}

// announceNodes picks who advertises. An explicit list wins; otherwise Ready
// workers, sorted.
func (r *AnnouncementPolicyReconciler) announceNodes(
	ctx context.Context, policy *platformv1alpha1.AnnouncementPolicy,
) ([]string, error) {
	replicas := int(policy.Spec.Replicas)
	if replicas < 1 {
		replicas = 2
	}

	if len(policy.Spec.Nodes) > 0 {
		explicit := append([]string(nil), policy.Spec.Nodes...)
		sort.Strings(explicit)
		if len(explicit) > replicas {
			explicit = explicit[:replicas]
		}
		return explicit, nil
	}

	nodes := &corev1.NodeList{}
	if err := r.List(ctx, nodes); err != nil {
		return nil, fmt.Errorf("listing nodes: %w", err)
	}

	var ready []string
	for i := range nodes.Items {
		node := &nodes.Items[i]
		// The border peers with workers. Sorting over every node put the
		// control plane first, and every prefix silently vanished from the
		// border while the generated object looked perfect.
		if _, isCP := node.Labels["node-role.kubernetes.io/control-plane"]; isCP {
			continue
		}
		for _, cond := range node.Status.Conditions {
			if cond.Type == corev1.NodeReady && cond.Status == corev1.ConditionTrue {
				ready = append(ready, node.Name)
				break
			}
		}
	}
	sort.Strings(ready)
	if len(ready) > replicas {
		ready = ready[:replicas]
	}
	return ready, nil
}

// writeConfiguration writes the FRRConfiguration, and only when it differs.
//
// Every write is a reload, and a reload is the one moment a session can flap.
func (r *AnnouncementPolicyReconciler) writeConfiguration(
	ctx context.Context,
	policy *platformv1alpha1.AnnouncementPolicy,
	announcements []announce.Announcement,
	nodes []string,
) error {
	namespace := policy.Spec.TargetNamespace
	if namespace == "" {
		namespace = "metallb-system"
	}

	nodeValues := make([]any, 0, len(nodes))
	for _, n := range nodes {
		nodeValues = append(nodeValues, n)
	}
	desiredSpec := map[string]any{
		"nodeSelector": map[string]any{
			"matchExpressions": []any{map[string]any{
				"key":      "kubernetes.io/hostname",
				"operator": "In",
				"values":   nodeValues,
			}},
		},
		"raw": map[string]any{
			"priority": int64(10),
			"rawConfig": announce.RenderRawConfig(
				announcements, policy.Spec.BorderPeer,
				policy.Spec.LocalASN, policy.Spec.PeerASN),
		},
	}

	existing := &unstructured.Unstructured{}
	existing.SetGroupVersionKind(frrConfigGVK)
	err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: frrConfigName}, existing)
	switch {
	case apierrors.IsNotFound(err):
		created := &unstructured.Unstructured{}
		created.SetGroupVersionKind(frrConfigGVK)
		created.SetName(frrConfigName)
		created.SetNamespace(namespace)
		created.SetLabels(map[string]string{"kubevirt-ui.io/managed": "true"})
		if err := unstructured.SetNestedMap(created.Object, desiredSpec, "spec"); err != nil {
			return fmt.Errorf("rendering the configuration: %w", err)
		}
		if err := r.Create(ctx, created); err != nil {
			return fmt.Errorf("creating %s/%s: %w", namespace, frrConfigName, err)
		}
		kube.CountWrite(r.Scheme, created, announceControllerName, "created")
		return nil
	case err != nil:
		return fmt.Errorf("reading %s/%s: %w", namespace, frrConfigName, err)
	}

	current, _, _ := unstructured.NestedMap(existing.Object, "spec")
	if equalSpecs(current, desiredSpec) {
		return nil
	}
	patched := existing.DeepCopy()
	if err := unstructured.SetNestedMap(patched.Object, desiredSpec, "spec"); err != nil {
		return fmt.Errorf("rendering the configuration: %w", err)
	}
	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("updating %s/%s: %w", namespace, frrConfigName, err)
	}
	kube.CountWrite(r.Scheme, patched, announceControllerName, "updated")
	return nil
}

// reloadFailures asks each node whether FRR took the configuration.
func (r *AnnouncementPolicyReconciler) reloadFailures(
	ctx context.Context, nodes []string,
) ([]platformv1alpha1.NodeReloadFailure, error) {
	var failures []platformv1alpha1.NodeReloadFailure
	for _, node := range nodes {
		state := &unstructured.Unstructured{}
		state.SetGroupVersionKind(frrNodeStateGVK)
		if err := r.Get(ctx, types.NamespacedName{Name: node}, state); err != nil {
			if apierrors.IsNotFound(err) {
				continue
			}
			return nil, fmt.Errorf("reading the FRR state of %s: %w", node, err)
		}
		// Both fields matter: a configuration can be rejected before it is ever
		// reloaded.
		for _, field := range []string{"lastConversionResult", "lastReloadResult"} {
			result, _, _ := unstructured.NestedString(state.Object, "status", field)
			result = strings.TrimSpace(result)
			if result == "" || result == "success" {
				continue
			}
			first := strings.SplitN(result, "\n", 2)[0]
			if len(first) > 200 {
				first = first[:200]
			}
			failures = append(failures, platformv1alpha1.NodeReloadFailure{
				Node: node, Message: field + ": " + first,
			})
			break
		}
	}
	return failures, nil
}

func toAnnouncedPrefixes(in []announce.Announcement) []platformv1alpha1.AnnouncedPrefix {
	if len(in) == 0 {
		return nil
	}
	out := make([]platformv1alpha1.AnnouncedPrefix, 0, len(in))
	for _, a := range in {
		out = append(out, platformv1alpha1.AnnouncedPrefix{
			VPC: a.VPC, CIDR: a.CIDR, NextHop: a.NextHop,
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].VPC != out[j].VPC {
			return out[i].VPC < out[j].VPC
		}
		return out[i].CIDR < out[j].CIDR
	})
	return out
}

func (r *AnnouncementPolicyReconciler) setAccepted(
	policy *platformv1alpha1.AnnouncementPolicy, ok bool, reason, message string,
) {
	status := metav1.ConditionTrue
	if !ok {
		status = metav1.ConditionFalse
	}
	apimeta.SetStatusCondition(&policy.Status.Conditions, metav1.Condition{
		Type:               platformv1alpha1.ConditionAccepted,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: policy.Generation,
	})
}

// SetupWithManager wires the controller to everything that can change what is
// advertised.
func (r *AnnouncementPolicyReconciler) SetupWithManager(mgr ctrl.Manager) error {
	toPolicy := handler.EnqueueRequestsFromMapFunc(
		func(ctx context.Context, _ client.Object) []reconcile.Request {
			list := &platformv1alpha1.AnnouncementPolicyList{}
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
	vpcs := &unstructured.Unstructured{}
	vpcs.SetGroupVersionKind(vpcGVK)
	eips := &unstructured.Unstructured{}
	eips.SetGroupVersionKind(ovnEipGVK)
	nodeStates := &unstructured.Unstructured{}
	nodeStates.SetGroupVersionKind(frrNodeStateGVK)

	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.AnnouncementPolicy{}).
		// Everything a network's right to be advertised depends on: the routes
		// it has, the leg it sits on, the subnets it owns, the nodes that speak
		// for it, and whether FRR took the last attempt.
		Watches(subnets, toPolicy).
		Watches(vpcs, toPolicy).
		Watches(eips, toPolicy).
		Watches(&corev1.Node{}, toPolicy).
		Watches(nodeStates, toPolicy).
		Named(announceControllerName).
		Complete(r)
}

// equalSpecs compares rendered specs. Values arrive from the API server as
// generic maps, so this is a deep comparison rather than a struct one.
func equalSpecs(a, b map[string]any) bool {
	return equality.Semantic.DeepEqual(a, b)
}
