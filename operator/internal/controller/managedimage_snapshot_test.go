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
	"fmt"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/cdi"
)

// A permanent snapshot is not a speed tweak. Measured on the pilot cluster:
// with the claim as the clone source CDI takes and throws away one
// `tmp-snapshot-*` per clone — two extra storage operations per machine — and
// under six concurrent creators that flow is what pushes the RBD node plugin
// into restarting. From a snapshot that already exists the same batch runs with
// none.
//
// So this file is about who decides. The image takes one snapshot and points
// its DataSource at it; every consumer names the DataSource and never learns
// which form is behind it. And every reason not to use a snapshot is written
// down on the image, because an installation that quietly took the slow path
// would be indistinguishable from one that took the fast one.

func storageClassNamed(name string) *string { return &name }

// mustStorageProfile fakes what CDI publishes about a storage class.
func mustStorageProfile(t *testing.T, name, provisioner, snapshotClass string) {
	t.Helper()
	profile := &cdiv1.StorageProfile{}
	profile.Name = name
	err := k8sClient.Create(testCtx, profile)
	if err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating StorageProfile %s: %v", name, err)
	}
	eventually(t, "the StorageProfile status to be published", func() error {
		live := &cdiv1.StorageProfile{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, live); err != nil {
			return err
		}
		live.Status.Provisioner = &provisioner
		if snapshotClass != "" {
			live.Status.SnapshotClass = &snapshotClass
		} else {
			live.Status.SnapshotClass = nil
		}
		return k8sClient.Status().Update(testCtx, live)
	})
}

// mustClaim stands in for the claim CDI's DataVolume would have produced. Its
// UID is the point: it is what the snapshot is tied to.
func mustClaim(t *testing.T, ns, name, class string) *corev1.PersistentVolumeClaim {
	t.Helper()
	claim := &corev1.PersistentVolumeClaim{}
	claim.Namespace, claim.Name = ns, name
	claim.Spec.AccessModes = []corev1.PersistentVolumeAccessMode{corev1.ReadWriteMany}
	claim.Spec.StorageClassName = storageClassNamed(class)
	claim.Spec.Resources = corev1.VolumeResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("10Gi")},
	}
	if err := k8sClient.Create(testCtx, claim); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating claim %s/%s: %v", ns, name, err)
	}
	live := &corev1.PersistentVolumeClaim{}
	eventually(t, "the claim to exist", func() error {
		return k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, live)
	})
	return live
}

func getSnapshot(ns, name string) (*unstructured.Unstructured, error) {
	snap := &unstructured.Unstructured{}
	snap.SetGroupVersionKind(cdi.VolumeSnapshotGVK)
	err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, snap)
	return snap, err
}

// markSnapshotUsable fakes the CSI snapshot controller, which envtest does not
// run.
func markSnapshotUsable(t *testing.T, ns, name string) {
	t.Helper()
	eventually(t, "the snapshot to exist before its status is faked", func() error {
		snap, err := getSnapshot(ns, name)
		if err != nil {
			return err
		}
		if err := unstructured.SetNestedField(snap.Object, true, "status", "readyToUse"); err != nil {
			return err
		}
		return k8sClient.Status().Update(testCtx, snap)
	})
}

func imageCondition(img *platformv1alpha1.ManagedImage, kind string) *metav1.Condition {
	return apimeta.FindStatusCondition(img.Status.Conditions, kind)
}

// imageOnClass drives one image all the way to an imported disk with a claim
// behind it, which is the state every case below starts from.
func imageOnClass(t *testing.T, ns, name, class string) *platformv1alpha1.ManagedImage {
	t.Helper()
	mustNamespace(t, ns, "opdev")
	img := newImage(ns, name)
	img.Spec.StorageClass = class
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, ns, name, cdiv1.Succeeded, nil, "100.0%")
	mustClaim(t, ns, name, class)
	return getImage(t, ns, name)
}

func TestImageTakesOneSnapshotAndPublishesItThroughTheDataSource(t *testing.T) {
	const class = "snap-capable"
	mustStorageProfile(t, class, "rbd.csi.example.com", "snap-capable")

	ns := "img-snapshot"
	imageOnClass(t, ns, "ubuntu-2404", class)
	claim := mustClaim(t, ns, "ubuntu-2404", class)

	eventually(t, "the snapshot to be taken from the image's own claim", func() error {
		snap, err := getSnapshot(ns, "ubuntu-2404")
		if err != nil {
			return err
		}
		from, _, _ := unstructured.NestedString(snap.Object,
			"spec", "source", "persistentVolumeClaimName")
		if from != "ubuntu-2404" {
			return fmt.Errorf("snapshot source claim = %q", from)
		}
		named, _, _ := unstructured.NestedString(snap.Object, "spec", "volumeSnapshotClassName")
		if named != class {
			return fmt.Errorf("volumeSnapshotClassName = %q, want the class CDI named", named)
		}
		// Tied to the volume, not to its name. A claim can come back under the
		// same name holding different content.
		if got := snap.GetAnnotations()[cdi.SourceClaimUIDAnnotation]; got != string(claim.UID) {
			return fmt.Errorf("snapshot records source claim %q, want %q", got, claim.UID)
		}
		return nil
	})

	// Until the snapshot is usable the DataSource must keep naming the claim:
	// a DataSource pointing at a snapshot that cannot yet be restored is an
	// outage for every consumer, and they have no way to see why.
	ds := &cdiv1.DataSource{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "ubuntu-2404"}, ds); err != nil {
		t.Fatalf("reading DataSource: %v", err)
	}
	if ds.Spec.Source.PVC == nil {
		t.Fatalf("DataSource left the claim before the snapshot was usable: %+v", ds.Spec.Source)
	}

	markSnapshotUsable(t, ns, "ubuntu-2404")

	eventually(t, "the DataSource to move to the snapshot", func() error {
		ds := &cdiv1.DataSource{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "ubuntu-2404"}, ds); err != nil {
			return err
		}
		if ds.Spec.Source.Snapshot == nil {
			return fmt.Errorf("DataSource still points at %+v", ds.Spec.Source)
		}
		if ds.Spec.Source.Snapshot.Name != "ubuntu-2404" || ds.Spec.Source.Snapshot.Namespace != ns {
			return fmt.Errorf("DataSource names snapshot %s/%s",
				ds.Spec.Source.Snapshot.Namespace, ds.Spec.Source.Snapshot.Name)
		}
		if ds.Spec.Source.PVC != nil {
			return fmt.Errorf("DataSource names both forms at once")
		}
		return nil
	})

	eventually(t, "the image to say which form is in effect", func() error {
		got := getImage(t, ns, "ubuntu-2404")
		if got.Status.CloneSource != "snapshot" {
			return fmt.Errorf("status.cloneSource = %q", got.Status.CloneSource)
		}
		if got.Status.SnapshotName != "ubuntu-2404" {
			return fmt.Errorf("status.snapshotName = %q", got.Status.SnapshotName)
		}
		cond := imageCondition(got, platformv1alpha1.ConditionSnapshotReady)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("SnapshotReady = %+v", cond)
		}
		return nil
	})
}

func TestStorageThatCannotSnapshotKeepsTheClaimAndSaysWhy(t *testing.T) {
	// No snapshot class for this provisioner anywhere in the cluster: the
	// supported case of an installation whose storage cannot snapshot.
	const class = "no-snapshots"
	mustStorageProfile(t, class, "nfs.csi.example.com", "")

	ns := "img-no-snapclass"
	imageOnClass(t, ns, "plain-disk", class)

	eventually(t, "the image to name the reason it clones from the claim", func() error {
		got := getImage(t, ns, "plain-disk")
		cond := imageCondition(got, platformv1alpha1.ConditionSnapshotReady)
		if cond == nil {
			return fmt.Errorf("no SnapshotReady condition at all")
		}
		if cond.Status != metav1.ConditionFalse || cond.Reason != "NoSnapshotClass" {
			return fmt.Errorf("SnapshotReady = %s/%s: %s", cond.Status, cond.Reason, cond.Message)
		}
		// The message has to be actionable on its own — the operator's log is
		// not where an administrator looks for why machines are slow.
		if !contains(cond.Message, "nfs.csi.example.com") || !contains(cond.Message, class) {
			return fmt.Errorf("message names neither the provisioner nor the class: %s", cond.Message)
		}
		if got.Status.CloneSource != "pvc" {
			return fmt.Errorf("status.cloneSource = %q", got.Status.CloneSource)
		}
		return nil
	})

	if _, err := getSnapshot(ns, "plain-disk"); !apierrors.IsNotFound(err) {
		t.Fatalf("a snapshot that can never become usable must not be created; got %v", err)
	}

	ds := &cdiv1.DataSource{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "plain-disk"}, ds); err != nil {
		t.Fatalf("reading DataSource: %v", err)
	}
	if ds.Spec.Source.PVC == nil {
		t.Fatalf("the fallback must still publish a usable DataSource, got %+v", ds.Spec.Source)
	}
}

func TestSnapshotOfAReplacedClaimIsNotTrusted(t *testing.T) {
	// The recovery path recreates a deleted image claim under the same name.
	// The old snapshot keeps working — measured on the pilot: a clone from a
	// snapshot whose source claim was deleted still succeeds, at the same
	// speed — so nothing looks wrong while it serves the previous volume's
	// content for ever. Only the UID separates the two.
	const class = "snap-capable"
	mustStorageProfile(t, class, "rbd.csi.example.com", "snap-capable")

	ns := "img-stale-snapshot"
	imageOnClass(t, ns, "recovered", class)
	markSnapshotUsable(t, ns, "recovered")

	eventually(t, "the image to settle on the snapshot", func() error {
		if got := getImage(t, ns, "recovered"); got.Status.CloneSource != "snapshot" {
			return fmt.Errorf("status.cloneSource = %q", got.Status.CloneSource)
		}
		return nil
	})

	snap, err := getSnapshot(ns, "recovered")
	if err != nil {
		t.Fatalf("reading the snapshot: %v", err)
	}
	firstUID := snap.GetUID()

	// Replace the claim: same name, different volume.
	claim := &corev1.PersistentVolumeClaim{}
	claim.Namespace, claim.Name = ns, "recovered"
	if err := k8sClient.Delete(testCtx, claim); err != nil {
		t.Fatalf("deleting the claim: %v", err)
	}
	// envtest keeps a deleted claim around while its protection finalizer is
	// on, and the claim never got bound here, so clearing it is what a real
	// cluster's controller would have done already.
	eventually(t, "the old claim to go", func() error {
		live := &corev1.PersistentVolumeClaim{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "recovered"}, live)
		if apierrors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if len(live.Finalizers) > 0 {
			live.Finalizers = nil
			return k8sClient.Update(testCtx, live)
		}
		return fmt.Errorf("still there")
	})
	mustClaim(t, ns, "recovered", class)

	// A recreated claim on its own produces no event this controller watches —
	// the real path recreates the DataVolume, which it does watch, and there is
	// a bounded recheck behind that. Neither is worth waiting for here: the
	// claim under test is what the next pass decides, so the next pass is what
	// the test asks for.
	nudge := getImage(t, ns, "recovered")
	nudge.Spec.Description = "nudged"
	if err := k8sClient.Update(testCtx, nudge); err != nil {
		t.Fatalf("nudging the image: %v", err)
	}

	eventually(t, "the stale snapshot to be replaced rather than reused", func() error {
		snap, err := getSnapshot(ns, "recovered")
		if apierrors.IsNotFound(err) {
			// Deleted; the next pass takes it again.
			return nil
		}
		if err != nil {
			return err
		}
		if snap.GetUID() == firstUID {
			return fmt.Errorf("the snapshot of the previous volume is still in place")
		}
		return nil
	})
}

func TestDeletionIsBlockedByAConsumerThatNamesTheDataSource(t *testing.T) {
	// The guard used to read `source.pvc` only. Moving consumers to `sourceRef`
	// would have made an image used by every machine in the cluster read as
	// used by nobody — a deletion guard that silently stopped guarding.
	ns := "img-sourceref-usedby"
	mustNamespace(t, ns, "opdev")
	img := newImage(ns, "shared-base")
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, ns, "shared-base", cdiv1.Succeeded, nil, "100.0%")

	consumerNS := ns
	consumer := &cdiv1.DataVolume{}
	consumer.Namespace, consumer.Name = consumerNS, "machine-root"
	sourceNS := ns
	consumer.Spec.SourceRef = &cdiv1.DataVolumeSourceRef{
		Kind: "DataSource", Name: "shared-base", Namespace: &sourceNS,
	}
	consumer.Spec.Storage = &cdiv1.StorageSpec{
		Resources: corev1.VolumeResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("10Gi")},
		},
	}
	if err := k8sClient.Create(testCtx, consumer); err != nil {
		t.Fatalf("creating the consumer: %v", err)
	}

	eventually(t, "the image to notice it is in use", func() error {
		got := getImage(t, ns, "shared-base")
		for _, u := range got.Status.UsedBy {
			if u == consumerNS+"/machine-root" {
				return nil
			}
		}
		return fmt.Errorf("status.usedBy = %v", got.Status.UsedBy)
	})
}
