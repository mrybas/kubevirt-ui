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
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/metrics"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

func newImage(ns, name string) *platformv1alpha1.ManagedImage {
	return &platformv1alpha1.ManagedImage{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: platformv1alpha1.ManagedImageSpec{
			DisplayName: "Ubuntu 24.04 Server",
			Source: platformv1alpha1.ManagedImageSource{
				HTTP: &platformv1alpha1.HTTPSource{
					URL: "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
				},
			},
			Size:  "10Gi",
			Scope: "environment",
		},
	}
}

// getImage reads through the manager's cache, which trails the API server by a
// beat. A create is visible to the controller (it is watching) before it is
// visible to a cache read here, so a bare Get right after Create is a coin
// flip — retry until it lands rather than turning cache lag into a red test.
func getImage(t *testing.T, ns, name string) *platformv1alpha1.ManagedImage {
	t.Helper()
	img := &platformv1alpha1.ManagedImage{}
	deadline := time.Now().Add(10 * time.Second)
	for {
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, img)
		if err == nil {
			return img
		}
		if !apierrors.IsNotFound(err) || time.Now().After(deadline) {
			t.Fatalf("reading ManagedImage %s/%s: %v", ns, name, err)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func getDV(ns, name string) (*cdiv1.DataVolume, error) {
	dv := &cdiv1.DataVolume{}
	err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, dv)
	return dv, err
}

// setDVStatus fakes what CDI would report. envtest runs no CDI controller, so
// every phase this suite reacts to has to be written by the test — and a fake
// that does not reproduce the measured behaviour of the real thing proves
// nothing, which is why the failure case below sets phase and condition the way
// CDI actually sets them.
func setDVStatus(t *testing.T, ns, name string, phase cdiv1.DataVolumePhase, conds []cdiv1.DataVolumeCondition, progress string) {
	t.Helper()
	eventually(t, "DataVolume "+name+" to exist before its status is faked", func() error {
		dv, err := getDV(ns, name)
		if err != nil {
			return err
		}
		dv.Status.Phase = phase
		dv.Status.Conditions = conds
		dv.Status.Progress = cdiv1.DataVolumeProgress(progress)
		return k8sClient.Status().Update(testCtx, dv)
	})
}

func TestImageCreatesDataVolumeAndDataSource(t *testing.T) {
	ns := "img-create"
	mustNamespace(t, ns, "opdev")

	img := newImage(ns, "ubuntu-2404")
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	eventually(t, "the DataVolume to be created", func() error {
		dv, err := getDV(ns, "ubuntu-2404")
		if err != nil {
			return err
		}
		// Parity with what the backend has always written: these labels are what
		// the UI's listers filter on, so a disk missing them is a disk the
		// product cannot see.
		want := map[string]string{
			naming.ManagedLabel:    "true",
			naming.DiskTypeLabel:   "image",
			naming.PersistentLabel: "false",
			naming.SlugLabel:       "ubuntu-24-04-server",
			naming.ScopeLabel:      "environment",
		}
		for k, v := range want {
			if dv.Labels[k] != v {
				return fmt.Errorf("label %s = %q, want %q", k, dv.Labels[k], v)
			}
		}
		if dv.Labels[naming.ProjectLabel] != "" {
			return fmt.Errorf("environment-scoped image must not carry a project label, got %q",
				dv.Labels[naming.ProjectLabel])
		}
		if dv.Annotations[naming.DisplayNameAnnotation] != "Ubuntu 24.04 Server" {
			return fmt.Errorf("display-name annotation = %q", dv.Annotations[naming.DisplayNameAnnotation])
		}
		if dv.Spec.Storage == nil || dv.Spec.Storage.VolumeMode == nil ||
			*dv.Spec.Storage.VolumeMode != corev1.PersistentVolumeBlock {
			return fmt.Errorf("volumeMode must be Block for snapshot-based cloning")
		}
		if dv.Spec.Source == nil || dv.Spec.Source.HTTP == nil {
			return fmt.Errorf("source is not an HTTP import")
		}
		return nil
	})

	eventually(t, "the DataSource to be published", func() error {
		ds := &cdiv1.DataSource{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "ubuntu-2404"}, ds); err != nil {
			return err
		}
		if ds.Spec.Source.PVC == nil || ds.Spec.Source.PVC.Name != "ubuntu-2404" {
			return fmt.Errorf("DataSource does not point at the image claim")
		}
		return nil
	})

	eventually(t, "status to name its children", func() error {
		got := getImage(t, ns, "ubuntu-2404")
		if got.Status.DataVolumeName != "ubuntu-2404" {
			return fmt.Errorf("status.dataVolumeName = %q", got.Status.DataVolumeName)
		}
		if got.Status.DataSourceName != "ubuntu-2404" {
			return fmt.Errorf("status.dataSourceName = %q", got.Status.DataSourceName)
		}
		return nil
	})
}

func TestProjectScopedImageCarriesProjectLabel(t *testing.T) {
	ns := "img-project-scope"
	mustNamespace(t, ns, "opdev")

	img := newImage(ns, "shared-image")
	img.Spec.Scope = "project"
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	eventually(t, "the project label to be stamped from the namespace", func() error {
		dv, err := getDV(ns, "shared-image")
		if err != nil {
			return err
		}
		if dv.Labels[naming.ProjectLabel] != "opdev" {
			return fmt.Errorf("project label = %q, want opdev", dv.Labels[naming.ProjectLabel])
		}
		return nil
	})
}

// CDI keeps a failing import in phase Pending while it retries, and reports the
// failure only on the Running condition. A controller that reads the phase
// alone shows a broken import as a slow one forever — this is the single most
// expensive misreading of CDI in the current product, and it is mapped in four
// separate places there.
func TestFailedImportIsReportedNotLeftPending(t *testing.T) {
	ns := "img-failure"
	mustNamespace(t, ns, "opdev")

	img := newImage(ns, "broken-image")
	img.Spec.Source.HTTP.URL = "https://example.invalid/nope.img"
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	setDVStatus(t, ns, "broken-image", cdiv1.Pending, []cdiv1.DataVolumeCondition{{
		Type:    cdiv1.DataVolumeRunning,
		Status:  corev1.ConditionFalse,
		Reason:  "Error",
		Message: "Unable to connect to http data source: dial tcp: no such host",
	}}, "")

	eventually(t, "the image to report Failed with the reason", func() error {
		got := getImage(t, ns, "broken-image")
		if got.Status.Phase != platformv1alpha1.ImagePhaseFailed {
			return fmt.Errorf("phase = %q, want Failed", got.Status.Phase)
		}
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionReady)
		if cond == nil || cond.Status != metav1.ConditionFalse {
			return fmt.Errorf("Ready condition missing or true")
		}
		if cond.Message == "" || cond.Message == "Disk has not been created yet" {
			return fmt.Errorf("failure carries no reason, message = %q", cond.Message)
		}
		return nil
	})
}

func TestSucceededImportBecomesReady(t *testing.T) {
	ns := "img-ready"
	mustNamespace(t, ns, "opdev")

	if err := k8sClient.Create(testCtx, newImage(ns, "good-image")); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, ns, "good-image", cdiv1.Succeeded, nil, "100.0%")

	eventually(t, "the image to become Ready", func() error {
		got := getImage(t, ns, "good-image")
		if got.Status.Phase != platformv1alpha1.ImagePhaseReady {
			return fmt.Errorf("phase = %q, want Ready", got.Status.Phase)
		}
		if got.Status.Progress != "100.0%" {
			return fmt.Errorf("progress = %q, want the CDI value passed through", got.Status.Progress)
		}
		if !apimeta.IsStatusConditionTrue(got.Status.Conditions, platformv1alpha1.ConditionReady) {
			return fmt.Errorf("Ready condition is not true")
		}
		return nil
	})
}

// Writing an object that already matches is not free: it bumps
// resourceVersion, wakes every watcher, and in the systems downstream of this
// operator a write means a reload. The counter is the only way to see the
// difference between a controller that settles and one that churns.
func TestSteadyStateStopsWriting(t *testing.T) {
	ns := "img-idempotent"
	mustNamespace(t, ns, "opdev")

	if err := k8sClient.Create(testCtx, newImage(ns, "quiet-image")); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, ns, "quiet-image", cdiv1.Succeeded, nil, "100.0%")

	eventually(t, "the image to settle on Ready", func() error {
		got := getImage(t, ns, "quiet-image")
		if got.Status.Phase != platformv1alpha1.ImagePhaseReady {
			return fmt.Errorf("phase = %q", got.Status.Phase)
		}
		return nil
	})
	// Let any reconcile triggered by the settling write drain before sampling.
	time.Sleep(2 * time.Second)

	baseline := totalPatches()
	consistently(t, "the write counter to stay flat once nothing changes", 5*time.Second, func() error {
		if now := totalPatches(); now != baseline {
			return fmt.Errorf("patches went from %v to %v with no input change", baseline, now)
		}
		return nil
	})
}

func totalPatches() float64 {
	var sum float64
	for _, op := range []string{"created", "updated", "deleted", "status"} {
		for _, kind := range []string{"DataVolume", "DataSource", "ManagedImage"} {
			sum += testutil.ToFloat64(metrics.PatchesTotal.WithLabelValues(kind, imageControllerName, op))
		}
	}
	return sum
}

// Deleting a disk that something is cloning from leaves the clone half-written
// and owned by nobody. The refusal has to name the holder, because "delete is
// stuck" without a name is the failure mode this whole status design exists to
// prevent.
func TestDeleteIsRefusedWhileTheImageIsInUse(t *testing.T) {
	ns := "img-in-use"
	mustNamespace(t, ns, "opdev")

	if err := k8sClient.Create(testCtx, newImage(ns, "busy-image")); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, ns, "busy-image", cdiv1.Succeeded, nil, "100.0%")

	// A VM's root disk: a DataVolume cloning from the image's claim, owned by
	// the VM — exactly what dataVolumeTemplates materialise into.
	consumer := &cdiv1.DataVolume{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "web-01-root-a1b2c3",
			Namespace: ns,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "kubevirt.io/v1",
				Kind:       "VirtualMachine",
				Name:       "web-01",
				UID:        types.UID("11111111-2222-3333-4444-555555555555"),
			}},
		},
		Spec: cdiv1.DataVolumeSpec{
			Source: &cdiv1.DataVolumeSource{
				PVC: &cdiv1.DataVolumeSourcePVC{Name: "busy-image", Namespace: ns},
			},
			Storage: &cdiv1.StorageSpec{},
		},
	}
	if err := k8sClient.Create(testCtx, consumer); err != nil {
		t.Fatalf("creating consumer: %v", err)
	}

	eventually(t, "the image to notice it is in use", func() error {
		got := getImage(t, ns, "busy-image")
		if len(got.Status.UsedBy) != 1 || got.Status.UsedBy[0] != ns+"/web-01" {
			return fmt.Errorf("usedBy = %v, want [%s/web-01]", got.Status.UsedBy, ns)
		}
		return nil
	})

	if err := k8sClient.Delete(testCtx, getImage(t, ns, "busy-image")); err != nil {
		t.Fatalf("deleting image: %v", err)
	}

	eventually(t, "the refusal to name the holder", func() error {
		got := getImage(t, ns, "busy-image")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionDeleting)
		if cond == nil {
			return fmt.Errorf("no Deleting condition")
		}
		if cond.Reason != "InUse" {
			return fmt.Errorf("reason = %q, want InUse", cond.Reason)
		}
		if !contains(cond.Message, "web-01") {
			return fmt.Errorf("message does not name the holder: %q", cond.Message)
		}
		return nil
	})

	// And the disk is still there — the refusal is real, not cosmetic.
	consistently(t, "the disk to survive a refused deletion", 3*time.Second, func() error {
		if _, err := getDV(ns, "busy-image"); err != nil {
			return fmt.Errorf("the disk was deleted anyway: %v", err)
		}
		return nil
	})

	// Once the consumer is gone the deletion completes on its own.
	if err := k8sClient.Delete(testCtx, consumer); err != nil {
		t.Fatalf("deleting consumer: %v", err)
	}
	eventually(t, "deletion to complete after the holder is gone", func() error {
		img := &platformv1alpha1.ManagedImage{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "busy-image"}, img)
		if err == nil {
			return fmt.Errorf("image still present with finalizer %v", img.Finalizers)
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	eventually(t, "the owned disk to be removed with its image", func() error {
		if _, err := getDV(ns, "busy-image"); err == nil {
			return fmt.Errorf("DataVolume outlived its ManagedImage")
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})
}

// Adoption is how an image that predates the operator comes under management.
// It must take the existing disk over, never quietly create a second one: the
// disk holds data, and "adopt" that provisions instead is a silent duplicate.
func TestAdoptionTakesOverAnExistingDiskWithoutRecreatingIt(t *testing.T) {
	ns := "img-adopt"
	mustNamespace(t, ns, "opdev")

	legacy := &cdiv1.DataVolume{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "ubuntu-2404-legacy-x7k2p",
			Namespace: ns,
			Labels:    map[string]string{naming.ManagedLabel: "true"},
		},
		Spec: cdiv1.DataVolumeSpec{
			Source:  &cdiv1.DataVolumeSource{Blank: &cdiv1.DataVolumeBlankImage{}},
			Storage: &cdiv1.StorageSpec{},
		},
	}
	if err := k8sClient.Create(testCtx, legacy); err != nil {
		t.Fatalf("creating legacy disk: %v", err)
	}
	legacyUID := legacy.UID

	img := newImage(ns, "ubuntu-2404")
	img.Annotations = map[string]string{naming.AdoptAnnotation: legacy.Name}
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	eventually(t, "the legacy disk to be stamped as owned", func() error {
		dv, err := getDV(ns, legacy.Name)
		if err != nil {
			return err
		}
		if dv.UID != legacyUID {
			return fmt.Errorf("the disk was recreated: uid changed")
		}
		if dv.Labels[naming.OwnerKindLabel] != "ManagedImage" {
			return fmt.Errorf("ownership was not stamped: %v", dv.Labels)
		}
		got := getImage(t, ns, "ubuntu-2404")
		if got.Status.DataVolumeName != legacy.Name {
			return fmt.Errorf("status points at %q, want the adopted disk", got.Status.DataVolumeName)
		}
		return nil
	})

	// And no second disk was provisioned under the image's own name.
	consistently(t, "no duplicate disk to appear", 3*time.Second, func() error {
		if _, err := getDV(ns, "ubuntu-2404"); err == nil {
			return fmt.Errorf("a second DataVolume was created next to the adopted one")
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})
}

// An adopt annotation pointing at nothing must say so. Creating a fresh disk
// instead would turn "take over that one" into "make a new one" — the same
// class of silence the image duplication bug came from.
func TestAdoptionOfAMissingDiskIsReportedNotInvented(t *testing.T) {
	ns := "img-adopt-missing"
	mustNamespace(t, ns, "opdev")

	img := newImage(ns, "ghost")
	img.Annotations = map[string]string{naming.AdoptAnnotation: "not-here"}
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	eventually(t, "the missing adopt target to be named", func() error {
		got := getImage(t, ns, "ghost")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionReady)
		if cond == nil || cond.Reason != "AdoptTargetMissing" {
			return fmt.Errorf("condition = %+v, want reason AdoptTargetMissing", cond)
		}
		if !contains(cond.Message, "not-here") {
			return fmt.Errorf("message does not name the target: %q", cond.Message)
		}
		return nil
	})

	consistently(t, "no disk to be invented", 3*time.Second, func() error {
		if _, err := getDV(ns, "ghost"); err == nil {
			return fmt.Errorf("a disk was created for an adoption that had no target")
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})
}

// Two images cannot own one disk. The second one must report the collision
// rather than reconcile over the first — this is the "two writers, one object"
// class, applied to ourselves before it can happen.
func TestASecondImageWillNotStealAnOwnedDisk(t *testing.T) {
	ns := "img-conflict"
	mustNamespace(t, ns, "opdev")

	if err := k8sClient.Create(testCtx, newImage(ns, "first")); err != nil {
		t.Fatalf("creating first image: %v", err)
	}
	eventually(t, "the first image to own its disk", func() error {
		dv, err := getDV(ns, "first")
		if err != nil {
			return err
		}
		if dv.Labels[naming.OwnerUIDLabel] == "" {
			return fmt.Errorf("no ownership stamp yet")
		}
		return nil
	})

	thief := newImage(ns, "thief")
	thief.Annotations = map[string]string{naming.AdoptAnnotation: "first"}
	if err := k8sClient.Create(testCtx, thief); err != nil {
		t.Fatalf("creating second image: %v", err)
	}

	eventually(t, "the collision to be reported", func() error {
		got := getImage(t, ns, "thief")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionReady)
		if cond == nil || cond.Reason != "DataVolumeConflict" {
			return fmt.Errorf("condition = %+v, want reason DataVolumeConflict", cond)
		}
		return nil
	})

	// The first image keeps its disk, unchanged.
	dv, err := getDV(ns, "first")
	if err != nil {
		t.Fatalf("reading the contested disk: %v", err)
	}
	owner := getImage(t, ns, "first")
	if dv.Labels[naming.OwnerUIDLabel] != string(owner.UID) {
		t.Fatalf("ownership was taken over: label %q, owner uid %q",
			dv.Labels[naming.OwnerUIDLabel], owner.UID)
	}
}

func TestPauseStopsReconciliationWithoutDeletingAnything(t *testing.T) {
	ns := "img-paused"
	mustNamespace(t, ns, "opdev")

	img := newImage(ns, "frozen")
	img.Annotations = map[string]string{pausedAnnotation: "true"}
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}

	consistently(t, "a paused image to be left alone", 4*time.Second, func() error {
		if _, err := getDV(ns, "frozen"); err == nil {
			return fmt.Errorf("a paused image was reconciled anyway")
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	// Unpausing resumes it: pause is a hold, not a tombstone.
	got := getImage(t, ns, "frozen")
	delete(got.Annotations, pausedAnnotation)
	if err := k8sClient.Update(testCtx, got); err != nil {
		t.Fatalf("unpausing: %v", err)
	}
	eventually(t, "the disk to appear after unpausing", func() error {
		_, err := getDV(ns, "frozen")
		return err
	})
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (haystack == needle ||
		len(needle) == 0 || indexOf(haystack, needle) >= 0)
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

var _ = client.ObjectKeyFromObject
