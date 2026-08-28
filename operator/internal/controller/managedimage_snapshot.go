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
	"os"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/cdi"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

// cloneSourceEnv turns the permanent snapshot off for an installation that
// wants the old behaviour back without waiting for a release. Values: anything
// but "pvc" keeps the snapshot.
//
// Read through a function on every pass rather than bound to a package
// variable: a value captured at import time is a value nobody can change, and
// this operator has already shipped one of those.
const cloneSourceEnv = "IMAGE_CLONE_SOURCE"

// volumeSnapshotClassListGVK is only ever listed, never written.
var volumeSnapshotClassListGVK = schema.GroupVersionKind{
	Group: "snapshot.storage.k8s.io", Version: "v1", Kind: "VolumeSnapshotClassList",
}

func snapshotSourceWanted() bool {
	return os.Getenv(cloneSourceEnv) != "pvc"
}

// snapshotOutcome is what one pass concluded about the permanent snapshot.
type snapshotOutcome struct {
	// Name is the snapshot to clone from, or "" to clone from the claim.
	Name string
	// Condition explains the choice, including when the choice is the claim.
	Condition metav1.Condition
	// Requeue asks for another look: something is still being taken.
	Requeue bool
}

// reconcileSnapshot maintains one permanent VolumeSnapshot per image, and says
// whether clones should come from it.
//
// The snapshot is not an optimisation of this controller's own work — it makes
// no difference to the import. It is there for everyone downstream: with a
// claim as the clone source CDI takes a throwaway snapshot for every single
// clone, which is twice the storage operations per machine and, under load,
// the difference between a node plugin that copes and one that restarts.
//
// Every reason to fall back to the claim is reported rather than assumed. An
// installation whose storage class has no VolumeSnapshotClass — or a cluster
// with no snapshot CRDs at all — is a supported installation, not a broken
// one; it simply keeps the older, costlier path, and the condition says which
// path is in effect and why.
func (r *ManagedImageReconciler) reconcileSnapshot(
	ctx context.Context,
	img *platformv1alpha1.ManagedImage,
	dv *cdiv1.DataVolume,
	projectName string,
) (snapshotOutcome, error) {
	if !snapshotSourceWanted() {
		// Deliberately does not delete the snapshot it stops using. A clone may
		// be restoring from it at this moment, and the kill switch exists to
		// change behaviour, not to destroy data on the way past.
		return snapshotOutcome{Condition: snapshotCondition(img, metav1.ConditionFalse, "Disabled",
			fmt.Sprintf("%s=pvc, so clones are taken from the claim; any snapshot already "+
				"taken is left in place and goes away with the image", cloneSourceEnv))}, nil
	}

	if img.Status.Phase != platformv1alpha1.ImagePhaseReady {
		return snapshotOutcome{Condition: snapshotCondition(img, metav1.ConditionFalse, "Importing",
			"the disk is not finished, and a snapshot of an unfinished disk would "+
				"be a broken clone source")}, nil
	}

	claim := &corev1.PersistentVolumeClaim{}
	err := r.Get(ctx, types.NamespacedName{Namespace: img.Namespace, Name: dv.Name}, claim)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return snapshotOutcome{Requeue: true, Condition: snapshotCondition(img,
				metav1.ConditionFalse, "ClaimMissing",
				fmt.Sprintf("DataVolume %s/%s reports Ready but its claim is not there yet",
					img.Namespace, dv.Name))}, nil
		}
		return snapshotOutcome{}, fmt.Errorf("reading the image claim %s/%s: %w",
			img.Namespace, dv.Name, err)
	}

	class, reason, message := r.snapshotClassFor(ctx, claim)
	if class == "" {
		return snapshotOutcome{Condition: snapshotCondition(img,
			metav1.ConditionFalse, reason, message)}, nil
	}

	existing := &unstructured.Unstructured{}
	existing.SetGroupVersionKind(cdi.VolumeSnapshotGVK)
	err = r.Get(ctx, types.NamespacedName{Namespace: img.Namespace, Name: img.Name}, existing)
	switch {
	case err == nil:
		if owner := existing.GetLabels()[naming.OwnerUIDLabel]; owner != string(img.UID) {
			// Someone else's snapshot under our name. Using it would mean
			// cloning content this image never imported.
			return snapshotOutcome{Condition: snapshotCondition(img,
				metav1.ConditionFalse, "SnapshotConflict",
				fmt.Sprintf("VolumeSnapshot %s/%s was not taken by this image; "+
					"clones keep coming from the claim", img.Namespace, img.Name))}, nil
		}
		if from := existing.GetAnnotations()[cdi.SourceClaimUIDAnnotation]; from != string(claim.UID) {
			// The disk was replaced under the same name — the recovery path
			// does exactly this. The snapshot still works, which is the whole
			// danger: it would serve the previous volume's content for ever.
			if err := kube.Delete(ctx, r.Client, imageControllerName, existing); err != nil {
				return snapshotOutcome{}, fmt.Errorf("removing the stale snapshot %s/%s: %w",
					img.Namespace, img.Name, err)
			}
			r.event(img, corev1.EventTypeNormal, "SnapshotRetaken",
				"The image's claim was replaced, so its snapshot was too")
			return snapshotOutcome{Requeue: true, Condition: snapshotCondition(img,
				metav1.ConditionFalse, "Retaking",
				"the image's claim was replaced, so the snapshot of the previous "+
					"volume was removed and is being taken again")}, nil
		}
		if ready, _, _ := unstructured.NestedBool(existing.Object, "status", "readyToUse"); !ready {
			// A snapshot that cannot be taken says so on itself. Repeating
			// "not usable yet" for ever would turn a storage error into a wait
			// nobody can explain.
			detail, _, _ := unstructured.NestedString(existing.Object, "status", "error", "message")
			if detail != "" {
				return snapshotOutcome{Requeue: true, Condition: snapshotCondition(img,
					metav1.ConditionFalse, "SnapshotFailed",
					fmt.Sprintf("VolumeSnapshot %s/%s reports: %s. Clones come from the "+
						"claim until it succeeds", img.Namespace, img.Name, detail))}, nil
			}
			return snapshotOutcome{Requeue: true, Condition: snapshotCondition(img,
				metav1.ConditionFalse, "Taking",
				fmt.Sprintf("VolumeSnapshot %s/%s is not usable yet",
					img.Namespace, img.Name))}, nil
		}
		return snapshotOutcome{Name: img.Name, Condition: snapshotCondition(img,
			metav1.ConditionTrue, "Ready",
			fmt.Sprintf("clones are restored from VolumeSnapshot %s/%s",
				img.Namespace, img.Name))}, nil

	case apierrors.IsNotFound(err):
		desired := cdi.DesiredVolumeSnapshot(img, projectName, claim.Name, string(claim.UID), class)
		created := desired.DeepCopy()
		if _, err := kube.Ensure(ctx, r.Client, imageControllerName, created, func() error {
			created.SetLabels(desired.GetLabels())
			created.SetAnnotations(desired.GetAnnotations())
			spec, _, _ := unstructured.NestedMap(desired.Object, "spec")
			return unstructured.SetNestedMap(created.Object, spec, "spec")
		}); err != nil {
			return snapshotOutcome{}, fmt.Errorf("taking the snapshot %s/%s: %w",
				img.Namespace, img.Name, err)
		}
		r.event(img, corev1.EventTypeNormal, "SnapshotTaken",
			fmt.Sprintf("Took VolumeSnapshot %s so clones do not have to", img.Name))
		return snapshotOutcome{Requeue: true, Condition: snapshotCondition(img,
			metav1.ConditionFalse, "Taking",
			fmt.Sprintf("VolumeSnapshot %s/%s is being taken", img.Namespace, img.Name))}, nil

	case noSuchType(err):
		return snapshotOutcome{Condition: snapshotCondition(img,
			metav1.ConditionFalse, "NoSnapshotSupport",
			"this cluster has no VolumeSnapshot type, so clones are taken from "+
				"the claim; install a CSI snapshot controller to halve the "+
				"storage operations per machine")}, nil

	default:
		return snapshotOutcome{}, fmt.Errorf("reading the snapshot %s/%s: %w",
			img.Namespace, img.Name, err)
	}
}

// snapshotClassFor answers which VolumeSnapshotClass the claim's storage class
// snapshots with, or why there is none.
//
// The first answer comes from CDI's own StorageProfile, because CDI is what
// will perform the clone and a class picked here by a different rule would be a
// second opinion on a question with one right answer.
//
// An empty `status.snapshotClass` is not taken as an answer either way. CDI's
// own field documentation says a class is then chosen by provisioner, but that
// is documentation, not a measurement, and reading the empty field as "this
// storage cannot snapshot" would infer a capability from a gap in one object.
// So the empty case is decided by what the cluster actually holds: if some
// VolumeSnapshotClass drives this provisioner, the snapshot is taken with no
// class named and the snapshot controller picks; if none does, taking one would
// only leave an object that can never become ready.
//
// Nothing here has to be right for the image to be correct. The DataSource
// keeps the claim form until a snapshot is genuinely usable, and a snapshot
// that fails or never settles is a named condition, not a broken image — so
// whatever CDI means by the empty field, the outcome is measured rather than
// predicted.
func (r *ManagedImageReconciler) snapshotClassFor(
	ctx context.Context, claim *corev1.PersistentVolumeClaim,
) (class, reason, message string) {
	name := ""
	if claim.Spec.StorageClassName != nil {
		name = *claim.Spec.StorageClassName
	}
	if name == "" {
		return "", "NoStorageClass",
			fmt.Sprintf("claim %s/%s names no storage class, so there is nothing to "+
				"look a snapshot class up by; clones come from the claim",
				claim.Namespace, claim.Name)
	}

	profile := &cdiv1.StorageProfile{}
	if err := r.Get(ctx, types.NamespacedName{Name: name}, profile); err != nil {
		if noSuchType(err) {
			return "", "NoStorageProfileType",
				"this cluster has no CDI StorageProfile type, so which storage can " +
					"snapshot is unknown; clones come from the claim"
		}
		return "", "NoStorageProfile",
			fmt.Sprintf("CDI has no StorageProfile for storage class %q, so whether it "+
				"can snapshot is unknown; clones come from the claim", name)
	}
	if profile.Status.SnapshotClass != nil && *profile.Status.SnapshotClass != "" {
		return *profile.Status.SnapshotClass, "", ""
	}

	provisioner := ""
	if profile.Status.Provisioner != nil {
		provisioner = *profile.Status.Provisioner
	}
	if provisioner == "" {
		return "", "UnknownProvisioner",
			fmt.Sprintf("CDI's StorageProfile for %q names no provisioner, so no snapshot "+
				"class can be matched to it; clones come from the claim", name)
	}

	classes := &unstructured.UnstructuredList{}
	classes.SetGroupVersionKind(volumeSnapshotClassListGVK)
	if err := r.List(ctx, classes); err != nil {
		if noSuchType(err) {
			return "", "NoSnapshotSupport",
				"this cluster has no VolumeSnapshotClass type, so clones are taken from " +
					"the claim; install a CSI snapshot controller to halve the storage " +
					"operations per machine"
		}
		return "", "SnapshotClassesUnreadable",
			fmt.Sprintf("could not read the cluster's snapshot classes (%v), so clones "+
				"keep coming from the claim", err)
	}
	for i := range classes.Items {
		driver, _, _ := unstructured.NestedString(classes.Items[i].Object, "driver")
		if driver == provisioner {
			// Deliberately unnamed: the snapshot controller's own default for
			// this driver is the class CDI would land on too.
			return cdi.SnapshotClassByDefault, "", ""
		}
	}
	return "", "NoSnapshotClass",
		fmt.Sprintf("nothing in this cluster snapshots %q, which is what storage class %q "+
			"provisions with, so clones come from the claim and cost a throwaway "+
			"snapshot each", provisioner, name)
}

func snapshotCondition(
	img *platformv1alpha1.ManagedImage, status metav1.ConditionStatus, reason, message string,
) metav1.Condition {
	return metav1.Condition{
		Type:               platformv1alpha1.ConditionSnapshotReady,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: img.Generation,
	}
}

// noSuchType is true when the cluster does not know the type at all, as opposed
// to knowing it and not having the object.
func noSuchType(err error) bool {
	return apimeta.IsNoMatchError(err) || runtime.IsNotRegisteredError(err)
}
