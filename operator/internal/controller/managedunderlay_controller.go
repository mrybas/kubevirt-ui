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
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
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
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/underlay"
)

const (
	underlayControllerName = "managedunderlay"

	// underlayResyncInterval is a backstop, not the mechanism. Label drift is
	// caught by the Node watch within seconds; this only covers a change no
	// object in this cluster reports — kube-ovn writing readyNodes without
	// bumping anything we watch, most of all.
	underlayResyncInterval = 5 * time.Minute
)

var (
	providerNetworkGVK = schema.GroupVersionKind{
		Group: "kubeovn.io", Version: "v1", Kind: "ProviderNetwork",
	}
	vlanGVK = schema.GroupVersionKind{Group: "kubeovn.io", Version: "v1", Kind: "Vlan"}
	nadGVK  = schema.GroupVersionKind{
		Group: "k8s.cni.cncf.io", Version: "v1", Kind: "NetworkAttachmentDefinition",
	}
)

// ManagedUnderlayReconciler builds and keeps the physical path VPC egress
// gateways attach to.
//
// The reason this is a controller and not an endpoint is one specific failure.
// The gateway node label was healed only when somebody opened the page: the
// heal lived on the GET. On the lab it was found at an explicit `false` on all
// three workers, with nothing in managedFields claiming it. The link-watcher
// DaemonSet selects on that label, so it scheduled nowhere — and `kubectl
// rollout status` still said "successfully rolled out", because zero desired
// pods are all ready. Nothing reported anything wrong until the links went down
// on their own, which took two hours and took the transit and egress planes
// with them.
//
// Here the same check runs on every pass, and a Node write wakes it.
type ManagedUnderlayReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedunderlays,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedunderlays/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeovn.io,resources=provider-networks;vlans;subnets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=k8s.cni.cncf.io,resources=network-attachment-definitions,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=apps,resources=daemonsets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=nodes,verbs=get;list;watch;patch;update
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch

// Reconcile builds the fabric and keeps the gateway label on the nodes.
func (r *ManagedUnderlayReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	u := &platformv1alpha1.ManagedUnderlay{}
	if err := r.Get(ctx, req.NamespacedName, u); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if u.Annotations[pausedAnnotation] == "true" || !u.DeletionTimestamp.IsZero() {
		// Children carry ownerReferences, so deletion is the API server's job.
		return ctrl.Result{}, nil
	}

	before := u.DeepCopy()

	kubeOVNNS, err := r.kubeOVNNamespace(ctx, u)
	if err != nil {
		return ctrl.Result{}, err
	}
	if kubeOVNNS == "" {
		r.setCondition(u, platformv1alpha1.ConditionFabricReady, false, "KubeOVNNotFound",
			"cannot find the namespace running "+underlay.KubeOVNCNIDaemonSet+
				"; set spec.kubeOVNNamespace")
		u.Status.ObservedGeneration = u.Generation
		return ctrl.Result{RequeueAfter: underlayResyncInterval},
			kube.UpdateStatus(ctx, r.Client, underlayControllerName, u, before)
	}

	u.Status.Provider = underlay.Provider(u, kubeOVNNS)

	if err := r.ensureFabric(ctx, u, kubeOVNNS); err != nil {
		r.setCondition(u, platformv1alpha1.ConditionFabricReady, false, "WriteFailed", err.Error())
		u.Status.ObservedGeneration = u.Generation
		_ = kube.UpdateStatus(ctx, r.Client, underlayControllerName, u, before)
		return ctrl.Result{}, err
	}
	r.setCondition(u, platformv1alpha1.ConditionFabricReady, true, "Built",
		fmt.Sprintf("ProviderNetwork/%s, Vlan/%s, NetworkAttachmentDefinition/%s/%s and Subnet/%s exist",
			underlay.ProviderNetworkName(u), underlay.VLANName(u),
			kubeOVNNS, underlay.SubnetName(u), underlay.SubnetName(u)))

	readyNodes, err := r.readyNodes(ctx, underlay.ProviderNetworkName(u))
	if err != nil {
		return ctrl.Result{}, err
	}
	u.Status.ReadyNodes = readyNodes

	if err := r.healGatewayLabels(ctx, u, readyNodes); err != nil {
		return ctrl.Result{}, err
	}

	daemonSets, err := r.ensureWorkarounds(ctx, u, kubeOVNNS)
	if err != nil {
		return ctrl.Result{}, err
	}
	u.Status.DaemonSets = daemonSets
	r.judgeWorkarounds(u, daemonSets)

	u.Status.ObservedGeneration = u.Generation
	if err := kube.UpdateStatus(ctx, r.Client, underlayControllerName, u, before); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: underlayResyncInterval}, nil
}

// ensureFabric writes the four objects a gateway needs, in dependency order:
// the Vlan references the ProviderNetwork, and the Subnet references both the
// Vlan and the NAD.
func (r *ManagedUnderlayReconciler) ensureFabric(
	ctx context.Context, u *platformv1alpha1.ManagedUnderlay, kubeOVNNS string,
) error {
	desired := []*unstructured.Unstructured{
		underlay.ProviderNetwork(u),
		underlay.Vlan(u),
		underlay.ExternalNAD(u, kubeOVNNS),
		underlay.ExternalSubnet(u, kubeOVNNS),
	}
	for _, want := range desired {
		if err := r.ensureUnstructured(ctx, u, want); err != nil {
			return err
		}
	}
	return nil
}

// ensureUnstructured creates or converges one fabric object.
//
// Only the fields this controller renders are compared and written. A blanket
// spec replacement would fight kube-ovn, which writes defaults back into the
// specs of its own objects — the two would rewrite each other forever, and
// every write to a Subnet is a chance for the dataplane to blink.
func (r *ManagedUnderlayReconciler) ensureUnstructured(
	ctx context.Context, u *platformv1alpha1.ManagedUnderlay, want *unstructured.Unstructured,
) error {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(want.GroupVersionKind())
	live.SetName(want.GetName())
	live.SetNamespace(want.GetNamespace())

	wantSpec, _, _ := unstructured.NestedMap(want.Object, "spec")
	wantLabels := want.GetLabels()

	_, err := kube.Ensure(ctx, r.Client, underlayControllerName, live, func() error {
		labels := live.GetLabels()
		if labels == nil {
			labels = map[string]string{}
		}
		for k, v := range wantLabels {
			labels[k] = v
		}
		live.SetLabels(labels)

		spec, _, _ := unstructured.NestedMap(live.Object, "spec")
		if spec == nil {
			spec = map[string]any{}
		}
		for k, v := range wantSpec {
			spec[k] = v
		}
		if err := unstructured.SetNestedMap(live.Object, spec, "spec"); err != nil {
			return err
		}
		return controllerutil.SetControllerReference(u, live, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("%s/%s: %w", want.GetKind(), want.GetName(), err)
	}
	return nil
}

// readyNodes are the nodes whose OVS bridge for this provider network came up.
//
// kube-ovn's own answer to "does this NIC exist here", and the only one worth
// trusting: the ProviderNetwork controller initialises the bridge per node and
// only then reports the node ready.
func (r *ManagedUnderlayReconciler) readyNodes(ctx context.Context, name string) ([]string, error) {
	pn := &unstructured.Unstructured{}
	pn.SetGroupVersionKind(providerNetworkGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: name}, pn); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("reading ProviderNetwork/%s: %w", name, err)
	}
	raw, _, _ := unstructured.NestedStringSlice(pn.Object, "status", "readyNodes")
	out := append([]string(nil), raw...)
	sort.Strings(out)
	return out, nil
}

// healGatewayLabels puts `ovn.kubernetes.io/external-gw=true` back on every
// ready node, every pass.
//
// Add-only, deliberately. A node dropping out of readyNodes is far more often
// kube-ovn being briefly unable to report than the NIC being gone, and stripping
// the label on that reading would take the link watcher down at exactly the
// moment the link needs watching. Labels are removed when the underlay is
// deleted, by whoever deletes it.
func (r *ManagedUnderlayReconciler) healGatewayLabels(
	ctx context.Context, u *platformv1alpha1.ManagedUnderlay, readyNodes []string,
) error {
	logger := log.FromContext(ctx)

	if len(readyNodes) == 0 {
		u.Status.LabelledNodes = nil
		r.setCondition(u, platformv1alpha1.ConditionNodesLabelled, false, "NoReadyNodes",
			fmt.Sprintf("ProviderNetwork/%s reports no ready nodes. Check that %s exists "+
				"on the workers and that the OVS bridge initialised "+
				"(ProviderNetwork .status.conditions).",
				underlay.ProviderNetworkName(u), u.Spec.Interface))
		return nil
	}

	var labelled, failed []string
	healed := 0
	for _, name := range readyNodes {
		node := &corev1.Node{}
		if err := r.Get(ctx, types.NamespacedName{Name: name}, node); err != nil {
			failed = append(failed, name)
			continue
		}
		if node.Labels[underlay.ExternalGWLabel] == "true" {
			labelled = append(labelled, name)
			continue
		}
		patched := node.DeepCopy()
		if patched.Labels == nil {
			patched.Labels = map[string]string{}
		}
		was := patched.Labels[underlay.ExternalGWLabel]
		patched.Labels[underlay.ExternalGWLabel] = "true"
		if err := r.Patch(ctx, patched, client.MergeFrom(node)); err != nil {
			logger.Error(err, "could not restore the gateway label", "node", name)
			failed = append(failed, name)
			continue
		}
		kube.CountWrite(r.Scheme, patched, underlayControllerName, "updated")
		healed++
		labelled = append(labelled, name)
		logger.Info("restored the gateway label",
			"node", name, "label", underlay.ExternalGWLabel, "was", was)
		r.event(u, corev1.EventTypeWarning, "GatewayLabelRestored",
			fmt.Sprintf("%s on node %s was %q; restored. The link watcher selects on it "+
				"and schedules nowhere without it.",
				underlay.ExternalGWLabel, name, was))
	}

	sort.Strings(labelled)
	u.Status.LabelledNodes = labelled
	u.Status.LabelHeals += int64(healed)

	if len(failed) > 0 {
		r.setCondition(u, platformv1alpha1.ConditionNodesLabelled, false, "LabelWriteFailed",
			"could not label: "+strings.Join(failed, ", "))
		return nil
	}
	r.setCondition(u, platformv1alpha1.ConditionNodesLabelled, true, "Labelled",
		fmt.Sprintf("%d node(s) carry the provider NIC: %s",
			len(labelled), strings.Join(labelled, ", ")))
	return nil
}

// ensureWorkarounds writes the two DaemonSets, or reports them as skipped.
//
// Neither is deleted when it is turned off. Both are cluster singletons shared
// by every underlay — there is one link watcher for every provider NIC — so a
// second underlay switching its own flag off must not take the first one's
// watcher with it. They are owned non-controller by each underlay that wants
// them, which is what makes the API server's garbage collector remove them only
// once the last such underlay is gone.
func (r *ManagedUnderlayReconciler) ensureWorkarounds(
	ctx context.Context, u *platformv1alpha1.ManagedUnderlay, kubeOVNNS string,
) ([]platformv1alpha1.UnderlayDaemonSetStatus, error) {
	var out []platformv1alpha1.UnderlayDaemonSetStatus

	if u.Spec.LinkWatcher == nil || *u.Spec.LinkWatcher {
		image, err := r.kubeOVNCNIImage(ctx, kubeOVNNS)
		if err != nil {
			return nil, err
		}
		others, err := r.allProviderInterfaces(ctx)
		if err != nil {
			return nil, err
		}
		want := underlay.LinkWatcher(u, kubeOVNNS, image,
			underlay.WatchedInterfaces(u.Spec.Interface, others))
		state, err := r.ensureDaemonSet(ctx, u, want)
		if err != nil {
			return nil, err
		}
		out = append(out, state)
	} else {
		out = append(out, platformv1alpha1.UnderlayDaemonSetStatus{
			Name: underlay.LinkWatcherName, Namespace: kubeOVNNS, State: "skipped",
			Detail: "linkWatcher=false — the provider NIC is assumed to stay up",
		})
	}

	chaining, ciliumNS, err := r.detectCilium(ctx)
	if err != nil {
		return nil, err
	}
	if u.Spec.CiliumNamespace != "" {
		ciliumNS = u.Spec.CiliumNamespace
	}
	wanted := chaining
	if u.Spec.CiliumSourceIPExempt != nil {
		wanted = *u.Spec.CiliumSourceIPExempt
	}
	switch {
	case wanted && ciliumNS == "":
		out = append(out, platformv1alpha1.UnderlayDaemonSetStatus{
			Name: underlay.CiliumExemptName, State: "absent",
			Detail: "asked for, but Cilium was not found; set spec.ciliumNamespace",
		})
	case wanted:
		state, err := r.ensureDaemonSet(ctx, u, underlay.CiliumExempt(u, ciliumNS))
		if err != nil {
			return nil, err
		}
		out = append(out, state)
	default:
		detail := "ciliumSourceIPExempt=false — asked for explicitly"
		if u.Spec.CiliumSourceIPExempt == nil {
			detail = "not needed — Cilium is not chaining on this cluster"
		}
		out = append(out, platformv1alpha1.UnderlayDaemonSetStatus{
			Name: underlay.CiliumExemptName, Namespace: ciliumNS,
			State: "skipped", Detail: detail,
		})
	}
	return out, nil
}

func (r *ManagedUnderlayReconciler) ensureDaemonSet(
	ctx context.Context, u *platformv1alpha1.ManagedUnderlay, want *appsv1.DaemonSet,
) (platformv1alpha1.UnderlayDaemonSetStatus, error) {
	live := &appsv1.DaemonSet{}
	live.Name = want.Name
	live.Namespace = want.Namespace

	_, err := kube.Ensure(ctx, r.Client, underlayControllerName, live, func() error {
		if live.Labels == nil {
			live.Labels = map[string]string{}
		}
		for k, v := range want.Labels {
			live.Labels[k] = v
		}
		if live.Annotations == nil {
			live.Annotations = map[string]string{}
		}
		for k, v := range want.Annotations {
			live.Annotations[k] = v
		}
		// The selector is immutable once set; writing the same value is a no-op
		// and writing a different one is rejected by the API server, which is
		// the correct outcome — a changed selector is a new DaemonSet.
		if live.Spec.Selector == nil {
			live.Spec.Selector = want.Spec.Selector
		}
		live.Spec.Template = want.Spec.Template
		// Non-controller: several underlays legitimately want the same
		// singleton, and SetControllerReference would refuse the second one.
		return controllerutil.SetOwnerReference(u, live, r.Scheme)
	})
	if err != nil {
		return platformv1alpha1.UnderlayDaemonSetStatus{}, fmt.Errorf(
			"DaemonSet/%s/%s: %w", want.Namespace, want.Name, err)
	}
	return daemonSetState(live), nil
}

// daemonSetState reports whether a DaemonSet is doing anything, as opposed to
// whether it exists.
//
// Both ways it can exist and do nothing are the same class of failure this
// fabric keeps producing: scheduled nowhere because no node carries the label,
// or scheduled and never started because the image stopped being pullable.
// Either one reads as a healthy DaemonSet in every summary that counts objects.
func daemonSetState(ds *appsv1.DaemonSet) platformv1alpha1.UnderlayDaemonSetStatus {
	desired := ds.Status.DesiredNumberScheduled
	ready := ds.Status.NumberReady
	state := platformv1alpha1.UnderlayDaemonSetStatus{
		Name: ds.Name, Namespace: ds.Namespace, Desired: desired, Ready: ready,
	}
	switch {
	case desired == 0:
		state.State = "scheduled-nowhere"
		state.Detail = "nothing matches its nodeSelector"
	case ready == 0:
		state.State = "not-starting"
		state.Detail = fmt.Sprintf("0/%d pods ready — check image pulls and events", desired)
	case ready < desired:
		state.State = "running"
		state.Detail = fmt.Sprintf("%d/%d ready", ready, desired)
	default:
		state.State = "running"
		state.Detail = fmt.Sprintf("%d/%d ready", ready, desired)
	}
	return state
}

func (r *ManagedUnderlayReconciler) judgeWorkarounds(
	u *platformv1alpha1.ManagedUnderlay, states []platformv1alpha1.UnderlayDaemonSetStatus,
) {
	var broken []string
	for _, s := range states {
		if s.State == "scheduled-nowhere" || s.State == "not-starting" || s.State == "absent" {
			broken = append(broken, fmt.Sprintf("%s (%s: %s)", s.Name, s.State, s.Detail))
		}
	}
	if len(broken) > 0 {
		r.setCondition(u, platformv1alpha1.ConditionWorkaroundsRunning, false, "NotRunning",
			strings.Join(broken, "; "))
		return
	}
	r.setCondition(u, platformv1alpha1.ConditionWorkaroundsRunning, true, "Running",
		"every workaround that is turned on has pods running")
}

// kubeOVNNamespace is where the NAD and the link watcher belong: wherever
// kube-ovn's own CNI DaemonSet runs.
func (r *ManagedUnderlayReconciler) kubeOVNNamespace(
	ctx context.Context, u *platformv1alpha1.ManagedUnderlay,
) (string, error) {
	if u.Spec.KubeOVNNamespace != "" {
		return u.Spec.KubeOVNNamespace, nil
	}
	list := &appsv1.DaemonSetList{}
	if err := r.List(ctx, list); err != nil {
		return "", fmt.Errorf("looking for %s: %w", underlay.KubeOVNCNIDaemonSet, err)
	}
	for i := range list.Items {
		if list.Items[i].Name == underlay.KubeOVNCNIDaemonSet {
			return list.Items[i].Namespace, nil
		}
	}
	return "", nil
}

func (r *ManagedUnderlayReconciler) kubeOVNCNIImage(ctx context.Context, ns string) (string, error) {
	ds := &appsv1.DaemonSet{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: ns, Name: underlay.KubeOVNCNIDaemonSet,
	}, ds)
	if apierrors.IsNotFound(err) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("reading %s/%s: %w", ns, underlay.KubeOVNCNIDaemonSet, err)
	}
	if len(ds.Spec.Template.Spec.Containers) == 0 {
		return "", nil
	}
	return ds.Spec.Template.Spec.Containers[0].Image, nil
}

// allProviderInterfaces is the default interface of every ProviderNetwork in
// the cluster, per-node overrides included: a node that carries the NIC under a
// different name still needs it raised.
func (r *ManagedUnderlayReconciler) allProviderInterfaces(ctx context.Context) ([]string, error) {
	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(providerNetworkGVK.GroupVersion().WithKind("ProviderNetworkList"))
	if err := r.List(ctx, list); err != nil {
		return nil, fmt.Errorf("listing ProviderNetworks for the link watcher: %w", err)
	}
	var out []string
	for i := range list.Items {
		obj := list.Items[i].Object
		if name, _, _ := unstructured.NestedString(obj, "spec", "defaultInterface"); name != "" {
			out = append(out, name)
		}
		customs, _, _ := unstructured.NestedSlice(obj, "spec", "customInterfaces")
		for _, raw := range customs {
			custom, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			if name, _ := custom["interface"].(string); name != "" {
				out = append(out, name)
			}
		}
	}
	return out, nil
}

// detectCilium answers both Cilium questions from the cluster: whether it
// chains, and where it lives.
//
// The form defaulted the first to "no" and the second to `kube-system`. On the
// cluster it was asked about, Cilium chains and lives in `o0-cilium`, so the
// build reported the workaround as "skipped — not chaining" while ticking the
// box by hand put the DaemonSet in an empty namespace. Both answers were in the
// cluster the whole time.
//
// The namespace is found from Cilium's own agent DaemonSet rather than by
// sweeping every ConfigMap in the cluster. A cluster-wide ConfigMap read is not
// free here the way it was from a request handler: this client is backed by an
// informer, so the same one-line answer would come at the cost of caching every
// ConfigMap in every namespace for the life of the process.
func (r *ManagedUnderlayReconciler) detectCilium(ctx context.Context) (bool, string, error) {
	list := &appsv1.DaemonSetList{}
	if err := r.List(ctx, list); err != nil {
		return false, "", fmt.Errorf("looking for the Cilium agent: %w", err)
	}
	namespace := ""
	for i := range list.Items {
		ds := &list.Items[i]
		if ds.Name == "cilium" || ds.Labels["k8s-app"] == "cilium" {
			namespace = ds.Namespace
			break
		}
	}
	if namespace == "" {
		return false, "", nil
	}

	cm := &corev1.ConfigMap{}
	err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: "cilium-config"}, cm)
	if apierrors.IsNotFound(err) {
		// Cilium is here and its configuration is not readable. Saying "not
		// chaining" would be a guess with a two-hour debugging session behind
		// it; the namespace is still worth returning so an explicit
		// ciliumSourceIPExempt lands in the right place.
		return false, namespace, nil
	}
	if err != nil {
		return false, namespace, fmt.Errorf("reading %s/cilium-config: %w", namespace, err)
	}
	mode := strings.ToLower(strings.TrimSpace(cm.Data["cni-chaining-mode"]))
	return mode != "" && mode != "none", namespace, nil
}

func (r *ManagedUnderlayReconciler) setCondition(
	u *platformv1alpha1.ManagedUnderlay, kind string, ok bool, reason, message string,
) {
	status := metav1.ConditionTrue
	if !ok {
		status = metav1.ConditionFalse
	}
	apimeta.SetStatusCondition(&u.Status.Conditions, metav1.Condition{
		Type:               kind,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: u.Generation,
	})
}

func (r *ManagedUnderlayReconciler) event(
	u *platformv1alpha1.ManagedUnderlay, kind, reason, message string,
) {
	if r.Recorder != nil {
		r.Recorder.Event(u, kind, reason, message)
	}
}

// SetupWithManager wires the controller to everything that can silently undo
// the fabric.
func (r *ManagedUnderlayReconciler) SetupWithManager(mgr ctrl.Manager) error {
	toUnderlays := handler.EnqueueRequestsFromMapFunc(
		func(ctx context.Context, _ client.Object) []reconcile.Request {
			list := &platformv1alpha1.ManagedUnderlayList{}
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

	providerNetworks := &unstructured.Unstructured{}
	providerNetworks.SetGroupVersionKind(providerNetworkGVK)
	vlans := &unstructured.Unstructured{}
	vlans.SetGroupVersionKind(vlanGVK)
	subnets := &unstructured.Unstructured{}
	subnets.SetGroupVersionKind(subnetGVK)
	nads := &unstructured.Unstructured{}
	nads.SetGroupVersionKind(nadGVK)

	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.ManagedUnderlay{}).
		// The Node watch is the whole point: a label edit is caught in seconds
		// rather than whenever somebody next opens the page. Filtered to label
		// changes, because a node also writes its status every few seconds and
		// none of that says anything about the fabric.
		Watches(&corev1.Node{}, toUnderlays,
			builder.WithPredicates(predicate.LabelChangedPredicate{})).
		Watches(providerNetworks, toUnderlays).
		Watches(vlans, toUnderlays).
		Watches(subnets, toUnderlays).
		Watches(nads, toUnderlays).
		// Only ours. Every other DaemonSet in the cluster writing its status is
		// not news to this controller.
		Watches(&appsv1.DaemonSet{}, toUnderlays,
			builder.WithPredicates(predicate.NewPredicateFuncs(func(o client.Object) bool {
				return o.GetLabels()[underlay.WorkaroundLabel] == "true"
			}))).
		Named(underlayControllerName).
		Complete(r)
}
