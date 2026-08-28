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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

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

	// cloneSourceIndex indexes DataVolumes by what they clone from, so "who is
	// using this image" is a cache lookup and not a cluster-wide scan on every
	// pass.
	//
	// Both forms land in one index. A consumer names either the claim
	// (`source.pvc`) or the image's DataSource (`sourceRef`), and an index that
	// knew only the first would have turned the move to `sourceRef` into a
	// silent loss of the deletion guard: an image in use by fifty machines
	// would have read as used by none.
	cloneSourceIndex = "spec.cloneSource"

	// blockedRequeue is how often a deletion held back by live consumers looks
	// again. Consumers disappear through their own deletions, which the watch
	// also sees; this is the backstop, not the mechanism.
	blockedRequeue = 30 * time.Second

	// snapshotPoll is how often an image looks again while its snapshot is
	// being taken. Nothing here watches VolumeSnapshots — the type is optional
	// in a cluster, and a watch on a type that may not exist stops the manager
	// from starting at all — so readiness is polled, and a snapshot takes
	// seconds rather than minutes.
	snapshotPoll = 5 * time.Second

	// snapshotRecheck bounds how long an image can keep serving a snapshot of a
	// volume that is no longer its own.
	//
	// The real replacement path recreates the DataVolume, which this controller
	// watches, so the usual detection is immediate. This is for the paths that
	// produce no event we see — a claim restored underneath us, an adoption —
	// because the failure it guards is silent by construction: the stale
	// snapshot keeps working, at full speed, serving the previous content.
	snapshotRecheck = 10 * time.Minute
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
// +kubebuilder:rbac:groups=cdi.kubevirt.io,resources=storageprofiles,verbs=get;list;watch
// +kubebuilder:rbac:groups=snapshot.storage.k8s.io,resources=volumesnapshots,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=snapshot.storage.k8s.io,resources=volumesnapshotclasses,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=persistentvolumeclaims,verbs=get;list;watch
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
		// The snapshot decides what the DataSource points at, so it is settled
		// first. The DataSource never names a snapshot that is not usable yet:
		// consumers read this one object, and a window where it resolves to
		// nothing would be an outage they could not explain.
		snap, err := r.reconcileSnapshot(ctx, img, dv, projectName)
		if err != nil {
			return ctrl.Result{}, err
		}
		apimeta.SetStatusCondition(&img.Status.Conditions, snap.Condition)
		img.Status.SnapshotName = snap.Name
		img.Status.CloneSource = "pvc"
		if snap.Name != "" {
			img.Status.CloneSource = "snapshot"
		}
		switch {
		case snap.Requeue && result.RequeueAfter == 0:
			result.RequeueAfter = snapshotPoll
		case snap.Name != "" && result.RequeueAfter == 0:
			result.RequeueAfter = snapshotRecheck
		}

		ds, err := r.reconcileDataSource(ctx, img, projectName, snap.Name)
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
	snapshotName string,
) (*cdiv1.DataSource, error) {
	desired := cdi.DesiredDataSource(img, projectName, snapshotName)
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
	snapshot := &unstructured.Unstructured{}
	snapshot.SetGroupVersionKind(cdi.VolumeSnapshotGVK)
	if err := r.deleteIfOwned(ctx, img, snapshot, img.Name); err != nil {
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
		// A type this cluster does not have holds nothing of ours, which is
		// the same situation as an object that is not there.
		if apierrors.IsNotFound(err) || noSuchType(err) {
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

	// Two keys, one for each way a consumer can name this image. The
	// DataSource carries the image's own name; the claim may differ from it
	// when the disk was adopted.
	keys := []string{img.Namespace + "/" + claim}
	if img.Name != claim {
		keys = append(keys, img.Namespace+"/"+img.Name)
	}

	var consumers cdiv1.DataVolumeList
	for _, key := range keys {
		page := &cdiv1.DataVolumeList{}
		if err := r.List(ctx, page, client.MatchingFields{cloneSourceIndex: key}); err != nil {
			return nil, err
		}
		consumers.Items = append(consumers.Items, page.Items...)
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
		context.Background(), &cdiv1.DataVolume{}, cloneSourceIndex,
		func(obj client.Object) []string {
			dv, ok := obj.(*cdiv1.DataVolume)
			if !ok {
				return nil
			}
			return cloneSourceKeys(dv)
		},
	); err != nil {
		return fmt.Errorf("indexing DataVolumes by clone source: %w", err)
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
	if !ok {
		return out
	}
	// The claim and the DataSource both share their name with the ManagedImage
	// that produced them, so either names the candidate directly. A request for
	// an image that does not exist costs one cache miss.
	for _, key := range cloneSourceKeys(dv) {
		ns, name, found := strings.Cut(key, "/")
		if !found {
			continue
		}
		out = append(out, reconcile.Request{
			NamespacedName: types.NamespacedName{Namespace: ns, Name: name},
		})
	}
	return out
}

// cloneSourceKeys is what a DataVolume clones from, as namespace/name, in
// whichever of the two forms it uses. One function so the index and the event
// mapping cannot drift apart — they are the same question asked twice.
func cloneSourceKeys(dv *cdiv1.DataVolume) []string {
	if dv.Spec.Source != nil && dv.Spec.Source.PVC != nil {
		ns := dv.Spec.Source.PVC.Namespace
		if ns == "" {
			ns = dv.Namespace
		}
		return []string{ns + "/" + dv.Spec.Source.PVC.Name}
	}
	if dv.Spec.SourceRef != nil && dv.Spec.SourceRef.Kind == "DataSource" {
		ns := dv.Namespace
		if dv.Spec.SourceRef.Namespace != nil && *dv.Spec.SourceRef.Namespace != "" {
			ns = *dv.Spec.SourceRef.Namespace
		}
		return []string{ns + "/" + dv.Spec.SourceRef.Name}
	}
	return nil
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
