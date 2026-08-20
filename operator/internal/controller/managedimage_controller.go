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
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/cdi"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

const (
	imageControllerName = "managedimage"

	// imageFinalizer holds deletion back while something is still cloning from
	// the disk. Removing a clone source mid-clone leaves a half-written disk
	// nobody owns.
	imageFinalizer = "platform.kubevirt-ui.io/managedimage"

	// pausedAnnotation stops reconciliation without deleting anything — the
	// escape hatch for the moment the operator is doing the wrong thing to a
	// live object.
	pausedAnnotation = "platform.kubevirt-ui.io/paused"

	// sourcePVCIndex indexes DataVolumes by the claim they clone from, so
	// "who is using this image" is a cache lookup and not a cluster-wide scan
	// on every pass.
	sourcePVCIndex = "spec.source.pvc"

	// blockedRequeue is how often a deletion held back by live consumers looks
	// again. Consumers disappear through their own deletions, which the watch
	// also sees; this is the backstop, not the mechanism.
	blockedRequeue = 30 * time.Second
)

// ManagedImageReconciler turns a ManagedImage into a CDI DataVolume plus the
// DataSource that names it.
type ManagedImageReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedimages,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedimages/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedimages/finalizers,verbs=update
// +kubebuilder:rbac:groups=cdi.kubevirt.io,resources=datavolumes,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=cdi.kubevirt.io,resources=datasources,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

// Reconcile brings one ManagedImage in line with the cluster.
func (r *ManagedImageReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	img := &platformv1alpha1.ManagedImage{}
	if err := r.Get(ctx, req.NamespacedName, img); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if img.Annotations[pausedAnnotation] == "true" {
		log.Info("Paused by annotation, not reconciling")
		return ctrl.Result{}, nil
	}

	before := img.DeepCopy()

	usedBy, err := r.usedBy(ctx, img)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("resolving image consumers: %w", err)
	}
	img.Status.UsedBy = usedBy

	if !img.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, img, before)
	}

	if !controllerutil.ContainsFinalizer(img, imageFinalizer) {
		controllerutil.AddFinalizer(img, imageFinalizer)
		if err := r.Update(ctx, img); err != nil {
			return ctrl.Result{}, fmt.Errorf("adding finalizer: %w", err)
		}
		// The update changed resourceVersion; come back with a fresh read
		// rather than writing status against a stale object.
		return ctrl.Result{Requeue: true}, nil
	}

	projectName, err := r.projectOf(ctx, img.Namespace)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("resolving project of namespace %s: %w", img.Namespace, err)
	}

	dv, result, blocked, err := r.reconcileDataVolume(ctx, img, projectName)
	if err != nil {
		return ctrl.Result{}, err
	}
	if blocked {
		// reconcileDataVolume already said, on the Ready condition, what is in
		// the way. Recomputing the status here would overwrite that reason with
		// a generic "not created yet" — the exact silence this design forbids.
		img.Status.ObservedGeneration = img.Generation
		if err := kube.UpdateStatus(ctx, r.Client, imageControllerName, img, before); err != nil {
			return ctrl.Result{}, fmt.Errorf("updating blocked status: %w", err)
		}
		return result, nil
	}

	phase, message := cdi.Interpret(dv)
	img.Status.Phase = phase
	img.Status.Progress = ""
	if dv != nil {
		img.Status.DataVolumeName = dv.Name
		img.Status.Progress = string(dv.Status.Progress)
	}

	// The DataSource is only meaningful once there is a claim behind it, but it
	// is harmless earlier and CDI reports its own readiness — publish it as soon
	// as the disk exists so consumers never have to wait on us to notice.
	if dv != nil {
		ds, err := r.reconcileDataSource(ctx, img, projectName)
		if err != nil {
			return ctrl.Result{}, err
		}
		if ds != nil {
			img.Status.DataSourceName = ds.Name
		}
	}

	setReadyCondition(img, phase, message)
	img.Status.ObservedGeneration = img.Generation

	if err := kube.UpdateStatus(ctx, r.Client, imageControllerName, img, before); err != nil {
		return ctrl.Result{}, fmt.Errorf("updating status: %w", err)
	}
	return result, nil
}

// reconcileDataVolume creates the disk, or takes over the one named by the
// adopt annotation. It never rewrites the spec of a disk that already exists:
// CDI does not re-import on spec changes, so a rewrite would only produce a
// difference between what the object says and what the disk holds.
func (r *ManagedImageReconciler) reconcileDataVolume(
	ctx context.Context,
	img *platformv1alpha1.ManagedImage,
	projectName string,
) (dv *cdiv1.DataVolume, result ctrl.Result, blocked bool, err error) {
	adopt := img.Annotations[naming.AdoptAnnotation]
	dvName := img.Name
	if adopt != "" {
		dvName = adopt
	}

	existing := &cdiv1.DataVolume{}
	err = r.Get(ctx, types.NamespacedName{Namespace: img.Namespace, Name: dvName}, existing)
	switch {
	case err == nil:
		// Someone else's disk is not ours to take. Saying so is the whole point:
		// silently reconciling over it is how two owners of one object start.
		if owner := existing.Labels[naming.OwnerUIDLabel]; owner != "" && owner != string(img.UID) {
			setBlockedCondition(img, "DataVolumeConflict",
				fmt.Sprintf("DataVolume %s/%s already belongs to another ManagedImage (owner-uid %s)",
					img.Namespace, dvName, owner))
			r.event(img, corev1.EventTypeWarning, "DataVolumeConflict",
				fmt.Sprintf("DataVolume %s is owned by another ManagedImage", dvName))
			return nil, ctrl.Result{}, true, nil
		}
		if !existing.DeletionTimestamp.IsZero() {
			// A name that is on its way out is not a name we can create yet.
			setBlockedCondition(img, "DataVolumeTerminating",
				fmt.Sprintf("DataVolume %s/%s is being deleted; waiting for the name to be free",
					img.Namespace, dvName))
			return nil, ctrl.Result{RequeueAfter: blockedRequeue}, true, nil
		}
		// Metadata is ours to keep current — labels drive the UI's filters and
		// the display name is editable. The spec is not touched.
		if err := r.adoptMetadata(ctx, img, existing, projectName); err != nil {
			return nil, ctrl.Result{}, false, err
		}
		return existing, ctrl.Result{}, false, nil

	case apierrors.IsNotFound(err):
		if adopt != "" {
			// Adoption names a specific object. Creating a fresh one instead
			// would quietly turn "take over that disk" into "make a new disk".
			setBlockedCondition(img, "AdoptTargetMissing",
				fmt.Sprintf("annotation %s names DataVolume %s/%s, which does not exist",
					naming.AdoptAnnotation, img.Namespace, adopt))
			return nil, ctrl.Result{RequeueAfter: blockedRequeue}, true, nil
		}
		desired, buildErr := cdi.DesiredDataVolume(img, projectName)
		if buildErr != nil {
			setBlockedCondition(img, "InvalidSpec", buildErr.Error())
			r.event(img, corev1.EventTypeWarning, "InvalidSpec", buildErr.Error())
			// Nothing about this fixes itself by retrying; wait for a spec edit.
			return nil, ctrl.Result{}, true, nil
		}
		created := desired.DeepCopy()
		if _, err := kube.Ensure(ctx, r.Client, imageControllerName, created, func() error {
			created.Labels = desired.Labels
			created.Annotations = desired.Annotations
			created.Spec = desired.Spec
			return nil
		}); err != nil {
			return nil, ctrl.Result{}, false, fmt.Errorf("creating DataVolume %s/%s: %w",
				img.Namespace, desired.Name, err)
		}
		r.event(img, corev1.EventTypeNormal, "DataVolumeCreated",
			fmt.Sprintf("Created DataVolume %s", created.Name))
		return created, ctrl.Result{}, false, nil

	default:
		return nil, ctrl.Result{}, false, fmt.Errorf("reading DataVolume %s/%s: %w",
			img.Namespace, dvName, err)
	}
}

// adoptMetadata keeps the product labels and annotations on an existing disk in
// sync, and stamps ownership. Spec is deliberately excluded.
func (r *ManagedImageReconciler) adoptMetadata(
	ctx context.Context,
	img *platformv1alpha1.ManagedImage,
	dv *cdiv1.DataVolume,
	projectName string,
) error {
	wantLabels := cdi.ImageLabels(img, projectName)
	wantAnnotations := cdi.ImageAnnotations(img)

	patched := dv.DeepCopy()
	if _, err := kube.Ensure(ctx, r.Client, imageControllerName, patched, func() error {
		if patched.Labels == nil {
			patched.Labels = map[string]string{}
		}
		for k, v := range wantLabels {
			patched.Labels[k] = v
		}
		if patched.Annotations == nil {
			patched.Annotations = map[string]string{}
		}
		for k, v := range wantAnnotations {
			patched.Annotations[k] = v
		}
		return nil
	}); err != nil {
		return fmt.Errorf("stamping ownership on DataVolume %s/%s: %w", dv.Namespace, dv.Name, err)
	}
	*dv = *patched
	return nil
}

func (r *ManagedImageReconciler) reconcileDataSource(
	ctx context.Context,
	img *platformv1alpha1.ManagedImage,
	projectName string,
) (*cdiv1.DataSource, error) {
	desired := cdi.DesiredDataSource(img, projectName)
	ds := desired.DeepCopy()
	if _, err := kube.Ensure(ctx, r.Client, imageControllerName, ds, func() error {
		if ds.Labels == nil {
			ds.Labels = map[string]string{}
		}
		for k, v := range desired.Labels {
			ds.Labels[k] = v
		}
		if ds.Annotations == nil {
			ds.Annotations = map[string]string{}
		}
		for k, v := range desired.Annotations {
			ds.Annotations[k] = v
		}
		ds.Spec = desired.Spec
		return nil
	}); err != nil {
		return nil, fmt.Errorf("ensuring DataSource %s/%s: %w", desired.Namespace, desired.Name, err)
	}
	return ds, nil
}

// reconcileDelete refuses while the image is in use, and otherwise removes the
// objects this controller owns before letting the resource go.
func (r *ManagedImageReconciler) reconcileDelete(
	ctx context.Context,
	img *platformv1alpha1.ManagedImage,
	before *platformv1alpha1.ManagedImage,
) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(img, imageFinalizer) {
		return ctrl.Result{}, nil
	}

	if len(img.Status.UsedBy) > 0 {
		msg := fmt.Sprintf("still in use by %v", img.Status.UsedBy)
		apimeta.SetStatusCondition(&img.Status.Conditions, metav1.Condition{
			Type:               platformv1alpha1.ConditionDeleting,
			Status:             metav1.ConditionFalse,
			Reason:             "InUse",
			Message:            msg,
			ObservedGeneration: img.Generation,
		})
		r.event(img, corev1.EventTypeWarning, "DeleteBlocked", msg)
		if err := kube.UpdateStatus(ctx, r.Client, imageControllerName, img, before); err != nil {
			return ctrl.Result{}, fmt.Errorf("updating status while blocked: %w", err)
		}
		return ctrl.Result{RequeueAfter: blockedRequeue}, nil
	}

	// Only objects carrying our ownership stamp are ours to remove. Anything
	// else in the way was put there by someone else, and a reconciler that
	// deletes what it did not create is worse than one that leaves litter.
	dvName := img.Status.DataVolumeName
	if dvName == "" {
		dvName = img.Name
	}
	if err := r.deleteIfOwned(ctx, img, &cdiv1.DataSource{}, img.Name); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.deleteIfOwned(ctx, img, &cdiv1.DataVolume{}, dvName); err != nil {
		return ctrl.Result{}, err
	}

	controllerutil.RemoveFinalizer(img, imageFinalizer)
	if err := r.Update(ctx, img); err != nil {
		return ctrl.Result{}, fmt.Errorf("removing finalizer: %w", err)
	}
	return ctrl.Result{}, nil
}

func (r *ManagedImageReconciler) deleteIfOwned(
	ctx context.Context,
	img *platformv1alpha1.ManagedImage,
	obj client.Object,
	name string,
) error {
	key := types.NamespacedName{Namespace: img.Namespace, Name: name}
	if err := r.Get(ctx, key, obj); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("reading %s for deletion: %w", key, err)
	}
	if obj.GetLabels()[naming.OwnerUIDLabel] != string(img.UID) {
		return nil
	}
	return kube.Delete(ctx, r.Client, imageControllerName, obj)
}

// usedBy answers "who would break if this disk disappeared", from the cache.
//
// Every consumer of a golden image ends up as a DataVolume that clones from its
// claim — a VM's dataVolumeTemplates materialise into exactly that. Objects on
// their way out do not count: a dying consumer that still blocked deletion
// would deadlock both.
func (r *ManagedImageReconciler) usedBy(
	ctx context.Context,
	img *platformv1alpha1.ManagedImage,
) ([]string, error) {
	claim := img.Status.DataVolumeName
	if claim == "" {
		claim = img.Name
	}

	consumers := &cdiv1.DataVolumeList{}
	if err := r.List(ctx, consumers,
		client.MatchingFields{sourcePVCIndex: img.Namespace + "/" + claim},
	); err != nil {
		return nil, err
	}

	seen := map[string]struct{}{}
	for i := range consumers.Items {
		dv := &consumers.Items[i]
		if !dv.DeletionTimestamp.IsZero() {
			continue
		}
		if dv.Labels[naming.OwnerUIDLabel] == string(img.UID) {
			continue
		}
		name := dv.Name
		for _, owner := range dv.OwnerReferences {
			// The VM name is the useful answer for a human deciding whether to
			// delete; the disk name it happens to have is not.
			if owner.Kind == "VirtualMachine" {
				name = owner.Name
				break
			}
		}
		seen[dv.Namespace+"/"+name] = struct{}{}
	}

	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

func (r *ManagedImageReconciler) projectOf(ctx context.Context, namespace string) (string, error) {
	ns := &corev1.Namespace{}
	if err := r.Get(ctx, types.NamespacedName{Name: namespace}, ns); err != nil {
		return "", err
	}
	return ns.Labels[naming.ProjectLabel], nil
}

func (r *ManagedImageReconciler) event(obj client.Object, eventType, reason, message string) {
	if r.Recorder == nil {
		return
	}
	r.Recorder.Event(obj, eventType, reason, message)
}

func setReadyCondition(img *platformv1alpha1.ManagedImage, phase, message string) {
	cond := metav1.Condition{
		Type:               platformv1alpha1.ConditionReady,
		ObservedGeneration: img.Generation,
	}
	switch phase {
	case platformv1alpha1.ImagePhaseReady:
		cond.Status = metav1.ConditionTrue
		cond.Reason = "Imported"
		cond.Message = "Disk is complete and can be cloned from"
	case platformv1alpha1.ImagePhaseFailed:
		cond.Status = metav1.ConditionFalse
		cond.Reason = "ImportFailed"
		cond.Message = message
	case platformv1alpha1.ImagePhaseImporting:
		cond.Status = metav1.ConditionFalse
		cond.Reason = "Importing"
		cond.Message = "CDI is still writing the disk"
	default:
		cond.Status = metav1.ConditionFalse
		cond.Reason = "Pending"
		cond.Message = "Disk has not been created yet"
	}
	apimeta.SetStatusCondition(&img.Status.Conditions, cond)
}

func setBlockedCondition(img *platformv1alpha1.ManagedImage, reason, message string) {
	img.Status.Phase = platformv1alpha1.ImagePhasePending
	apimeta.SetStatusCondition(&img.Status.Conditions, metav1.Condition{
		Type:               platformv1alpha1.ConditionReady,
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: img.Generation,
	})
}

// SetupWithManager wires the controller and the index that makes "who uses this
// image" cheap.
func (r *ManagedImageReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if err := mgr.GetFieldIndexer().IndexField(
		context.Background(), &cdiv1.DataVolume{}, sourcePVCIndex,
		func(obj client.Object) []string {
			dv, ok := obj.(*cdiv1.DataVolume)
			if !ok || dv.Spec.Source == nil || dv.Spec.Source.PVC == nil {
				return nil
			}
			ns := dv.Spec.Source.PVC.Namespace
			if ns == "" {
				ns = dv.Namespace
			}
			return []string{ns + "/" + dv.Spec.Source.PVC.Name}
		},
	); err != nil {
		return fmt.Errorf("indexing DataVolumes by source claim: %w", err)
	}

	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.ManagedImage{}).
		Watches(
			&cdiv1.DataVolume{},
			handler.EnqueueRequestsFromMapFunc(mapDataVolumeToImages),
			builder.WithPredicates(),
		).
		Watches(
			&cdiv1.DataSource{},
			handler.EnqueueRequestsFromMapFunc(mapOwnedToImage),
		).
		Named(imageControllerName).
		Complete(r)
}

// mapDataVolumeToImages routes a DataVolume event to the images that care:
// the image that owns it, and — because a clone changes who is using what —
// the image it clones from.
func mapDataVolumeToImages(_ context.Context, obj client.Object) []reconcile.Request {
	var out []reconcile.Request
	if req, ok := ownerRequest(obj); ok {
		out = append(out, req)
	}
	dv, ok := obj.(*cdiv1.DataVolume)
	if !ok || dv.Spec.Source == nil || dv.Spec.Source.PVC == nil {
		return out
	}
	ns := dv.Spec.Source.PVC.Namespace
	if ns == "" {
		ns = dv.Namespace
	}
	// The claim shares its name with the ManagedImage that produced it, so the
	// source claim names the candidate directly. A request for an image that
	// does not exist costs one cache miss.
	out = append(out, reconcile.Request{
		NamespacedName: types.NamespacedName{Namespace: ns, Name: dv.Spec.Source.PVC.Name},
	})
	return out
}

func mapOwnedToImage(_ context.Context, obj client.Object) []reconcile.Request {
	if req, ok := ownerRequest(obj); ok {
		return []reconcile.Request{req}
	}
	return nil
}

func ownerRequest(obj client.Object) (reconcile.Request, bool) {
	labels := obj.GetLabels()
	if labels[naming.OwnerKindLabel] != "ManagedImage" {
		return reconcile.Request{}, false
	}
	name := labels[naming.OwnerNameLabel]
	if name == "" {
		return reconcile.Request{}, false
	}
	return reconcile.Request{
		NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: name},
	}, true
}
