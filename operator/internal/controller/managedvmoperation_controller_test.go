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
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	clonev1beta1 "kubevirt.io/api/clone/v1beta1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	snapshotv1beta1 "kubevirt.io/api/snapshot/v1beta1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

func getOp(t *testing.T, ns, name string) *platformv1alpha1.ManagedVMOperation {
	t.Helper()
	op := &platformv1alpha1.ManagedVMOperation{}
	deadline := time.Now().Add(10 * time.Second)
	for {
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, op)
		if err == nil {
			return op
		}
		if !apierrors.IsNotFound(err) || time.Now().After(deadline) {
			t.Fatalf("reading operation %s/%s: %v", ns, name, err)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func mustSnapshot(t *testing.T, ns, name, vmName string) {
	t.Helper()
	snap := &snapshotv1beta1.VirtualMachineSnapshot{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: snapshotv1beta1.VirtualMachineSnapshotSpec{
			Source: corev1.TypedLocalObjectReference{
				APIGroup: ptrTo("kubevirt.io"),
				Kind:     "VirtualMachine",
				Name:     vmName,
			},
		},
	}
	if err := k8sClient.Create(testCtx, snap); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating snapshot: %v", err)
	}
}

// A restore stops the machine, and putting it back afterwards is the only part
// KubeVirt does not do. In the path this replaces that fact lived in a local
// variable inside an HTTP handler: lose the process midway and the machine
// stayed stopped for good, with nothing anywhere remembering otherwise.
func TestARestoreRemembersThePowerStateOnTheObject(t *testing.T) {
	ns := "op-restore"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	vm := newManagedVM(ns, "restorable", "ubuntu")
	vm.Spec.Running = true
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the machine to exist", func() error {
		_, err := getKubeVirtVM(ns, "restorable")
		return err
	})
	mustSnapshot(t, ns, "snap-1", "restorable")

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "restore-1", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:  "restorable",
			Action:  platformv1alpha1.OperationRestore,
			Restore: &platformv1alpha1.RestoreSpec{SnapshotName: "snap-1"},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	eventually(t, "the power state to be recorded before anything happens", func() error {
		got := getOp(t, ns, "restore-1")
		if got.Status.RunningBefore == nil {
			return fmt.Errorf("runningBefore not recorded")
		}
		if !*got.Status.RunningBefore {
			return fmt.Errorf("recorded the wrong state")
		}
		if got.Status.ChildName == "" {
			return fmt.Errorf("no restore object yet")
		}
		return nil
	})

	// The machine belongs to the operation while it runs.
	eventually(t, "the VM controller to yield", func() error {
		got := getVM(t, ns, "restorable")
		if got.Status.OperationInProgress != "restore-1" {
			return fmt.Errorf("operationInProgress = %q", got.Status.OperationInProgress)
		}
		return nil
	})

	// KubeVirt stops the machine to restore it. Nothing must put it back while
	// the restore is in flight.
	eventually(t, "the stop to stick", func() error {
		kvm, err := getKubeVirtVM(ns, "restorable")
		if err != nil {
			return err
		}
		if kvm.Spec.RunStrategy != nil && *kvm.Spec.RunStrategy == kubevirtv1.RunStrategyHalted {
			return nil
		}
		halted := kubevirtv1.RunStrategyHalted
		kvm.Spec.RunStrategy = &halted
		if err := k8sClient.Update(testCtx, kvm); err != nil {
			return err
		}
		// Reads here go through the manager's cache, which trails the write.
		// Asserting "nothing changed it" against a stale read would accuse the
		// controller of something the cache did.
		return fmt.Errorf("waiting for the stop to be visible")
	})
	consistently(t, "the machine to stay stopped during the restore", 4*time.Second, func() error {
		kvm, err := getKubeVirtVM(ns, "restorable")
		if err != nil {
			return err
		}
		if *kvm.Spec.RunStrategy != kubevirtv1.RunStrategyHalted {
			return fmt.Errorf("something restarted the machine mid-restore")
		}
		return nil
	})

	// KubeVirt finishes.
	eventually(t, "the restore to be marked complete", func() error {
		restore := &snapshotv1beta1.VirtualMachineRestore{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: getOp(t, ns, "restore-1").Status.ChildName,
		}, restore); err != nil {
			return err
		}
		done := true
		restore.Status = &snapshotv1beta1.VirtualMachineRestoreStatus{Complete: &done}
		return k8sClient.Status().Update(testCtx, restore)
	})

	eventually(t, "the operation to succeed and put the power state back", func() error {
		got := getOp(t, ns, "restore-1")
		if got.Status.Phase != platformv1alpha1.OperationPhaseSucceeded {
			return fmt.Errorf("phase = %q (%s)", got.Status.Phase, got.Status.Message)
		}
		vm := getVM(t, ns, "restorable")
		if !vm.Spec.Running {
			return fmt.Errorf("the machine was left stopped after a restore it started running")
		}
		return nil
	})

	eventually(t, "the machine to be released", func() error {
		got := getVM(t, ns, "restorable")
		if got.Status.OperationInProgress != "" {
			return fmt.Errorf("still yielded to %q", got.Status.OperationInProgress)
		}
		return nil
	})
}

// The old loop polled for two minutes without ever looking for a failure, and
// then returned success.
func TestAFailedRestoreIsReportedNotWaitedOut(t *testing.T) {
	ns := "op-restore-fail"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "doomed-restore", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the machine to exist", func() error {
		_, err := getKubeVirtVM(ns, "doomed-restore")
		return err
	})
	mustSnapshot(t, ns, "snap-bad", "doomed-restore")

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "restore-bad", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:  "doomed-restore",
			Action:  platformv1alpha1.OperationRestore,
			Restore: &platformv1alpha1.RestoreSpec{SnapshotName: "snap-bad"},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	eventually(t, "the restore object to appear", func() error {
		if getOp(t, ns, "restore-bad").Status.ChildName == "" {
			return fmt.Errorf("not yet")
		}
		return nil
	})

	eventually(t, "the failure to be published by KubeVirt", func() error {
		restore := &snapshotv1beta1.VirtualMachineRestore{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: getOp(t, ns, "restore-bad").Status.ChildName,
		}, restore); err != nil {
			return err
		}
		restore.Status = &snapshotv1beta1.VirtualMachineRestoreStatus{
			Conditions: []snapshotv1beta1.Condition{{
				Type:    snapshotv1beta1.ConditionFailure,
				Status:  corev1.ConditionTrue,
				Reason:  "VolumeRestoreFailed",
				Message: "the source volume is gone",
			}},
		}
		return k8sClient.Status().Update(testCtx, restore)
	})

	eventually(t, "the operation to fail, saying why", func() error {
		got := getOp(t, ns, "restore-bad")
		if got.Status.Phase != platformv1alpha1.OperationPhaseFailed {
			return fmt.Errorf("phase = %q", got.Status.Phase)
		}
		if !strings.Contains(got.Status.Message, "source volume is gone") {
			return fmt.Errorf("message does not carry the reason: %q", got.Status.Message)
		}
		return nil
	})
}

// The path this replaces set a nodeSelector on the machine to steer a
// migration and nothing ever removed it, so a VM migrated once could not leave
// that node again. The selector belongs on the migration.
func TestMigrationSteersTheMigrationNotTheMachine(t *testing.T) {
	ns := "op-migrate"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	vm := newManagedVM(ns, "mover", "ubuntu")
	vm.Spec.Running = true
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the machine to exist", func() error {
		_, err := getKubeVirtVM(ns, "mover")
		return err
	})

	// KubeVirt would create this; envtest has no virt-controller.
	vmi := &kubevirtv1.VirtualMachineInstance{
		ObjectMeta: metav1.ObjectMeta{Name: "mover", Namespace: ns},
		Spec:       kubevirtv1.VirtualMachineInstanceSpec{},
	}
	if err := k8sClient.Create(testCtx, vmi); err != nil {
		t.Fatalf("creating vmi: %v", err)
	}
	// VirtualMachineInstance has no status subresource in this KubeVirt build
	// (checked against the CRD, not assumed), so its status travels with the
	// object rather than through /status.
	vmi.Status.NodeName = "worker-1"
	if err := k8sClient.Update(testCtx, vmi); err != nil {
		t.Fatalf("setting the node: %v", err)
	}

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "migrate-1", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:  "mover",
			Action:  platformv1alpha1.OperationMigrate,
			Migrate: &platformv1alpha1.MigrateSpec{TargetNode: "worker-2"},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	eventually(t, "the migration to carry the target, not the machine", func() error {
		got := getOp(t, ns, "migrate-1")
		if got.Status.ChildName == "" {
			return fmt.Errorf("no migration yet")
		}
		migration := &kubevirtv1.VirtualMachineInstanceMigration{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: got.Status.ChildName,
		}, migration); err != nil {
			return err
		}
		if migration.Spec.AddedNodeSelector[corev1.LabelHostname] != "worker-2" {
			return fmt.Errorf("addedNodeSelector = %v", migration.Spec.AddedNodeSelector)
		}
		kvm, err := getKubeVirtVM(ns, "mover")
		if err != nil {
			return err
		}
		if sel := kvm.Spec.Template.Spec.NodeSelector; len(sel) != 0 {
			return fmt.Errorf("the machine was pinned to a node: %v", sel)
		}
		return nil
	})

	// And the machine is still not pinned once the migration finishes.
	eventually(t, "the migration to be marked done", func() error {
		migration := &kubevirtv1.VirtualMachineInstanceMigration{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: getOp(t, ns, "migrate-1").Status.ChildName,
		}, migration); err != nil {
			return err
		}
		migration.Status.Phase = kubevirtv1.MigrationSucceeded
		return k8sClient.Status().Update(testCtx, migration)
	})

	eventually(t, "the operation to succeed", func() error {
		got := getOp(t, ns, "migrate-1")
		if got.Status.Phase != platformv1alpha1.OperationPhaseSucceeded {
			return fmt.Errorf("phase = %q (%s)", got.Status.Phase, got.Status.Message)
		}
		return nil
	})

	kvm, err := getKubeVirtVM(ns, "mover")
	if err != nil {
		t.Fatalf("reading the machine: %v", err)
	}
	if sel := kvm.Spec.Template.Spec.NodeSelector; len(sel) != 0 {
		t.Fatalf("the machine was left pinned to %v", sel)
	}
}

func TestMigratingAStoppedMachineSaysSo(t *testing.T) {
	ns := "op-migrate-stopped"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "parked", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "migrate-parked", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:  "parked",
			Action:  platformv1alpha1.OperationMigrate,
			Migrate: &platformv1alpha1.MigrateSpec{},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	eventually(t, "the operation to refuse with a reason", func() error {
		got := getOp(t, ns, "migrate-parked")
		if got.Status.Phase != platformv1alpha1.OperationPhaseFailed {
			return fmt.Errorf("phase = %q", got.Status.Phase)
		}
		if !strings.Contains(got.Status.Message, "nothing to migrate") {
			return fmt.Errorf("message = %q", got.Status.Message)
		}
		return nil
	})
}

// An operation describes one act. Editing it after the fact would make the
// record of what happened disagree with what happened.
func TestAnOperationCannotBeEdited(t *testing.T) {
	ns := "op-immutable"
	mustNamespace(t, ns, "opdev")

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "frozen-op", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:  "whatever",
			Action:  platformv1alpha1.OperationMigrate,
			Migrate: &platformv1alpha1.MigrateSpec{TargetNode: "worker-1"},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}
	got := getOp(t, ns, "frozen-op")
	got.Spec.Migrate.TargetNode = "worker-3"
	if err := k8sClient.Update(testCtx, got); err == nil {
		t.Fatal("an operation's spec was edited after creation")
	}
}

func mustVolumeSnapshot(t *testing.T, ns, name, sourceClaim, size string, ready bool) {
	t.Helper()
	snap := &unstructured.Unstructured{}
	snap.SetGroupVersionKind(volumeSnapshotGVK)
	snap.SetName(name)
	snap.SetNamespace(ns)
	if err := unstructured.SetNestedMap(snap.Object, map[string]any{
		"source":                  map[string]any{"persistentVolumeClaimName": sourceClaim},
		"volumeSnapshotClassName": "csi-class",
	}, "spec"); err != nil {
		t.Fatalf("building snapshot: %v", err)
	}
	if err := k8sClient.Create(testCtx, snap); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating snapshot: %v", err)
	}
	eventually(t, "the snapshot to report itself usable", func() error {
		got := &unstructured.Unstructured{}
		got.SetGroupVersionKind(volumeSnapshotGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, got); err != nil {
			return err
		}
		if err := unstructured.SetNestedMap(got.Object, map[string]any{
			"readyToUse":  ready,
			"restoreSize": size,
		}, "status"); err != nil {
			return err
		}
		return k8sClient.Status().Update(testCtx, got)
	})
}

// The path this replaces deleted the claim and created its successor
// afterwards, so a process that died in between left a machine with no disk and
// no record of what it should have had. Here the replacement is built first,
// the machine is pointed at it, and only then is the old disk removed.
func TestARollbackReplacesTheDiskInsteadOfDestroyingIt(t *testing.T) {
	ns := "op-rollback"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustDataVolume(t, ns, "payload")
	mustVolumeSnapshot(t, ns, "payload-snap", "payload", "1Gi", true)

	vm := newManagedVM(ns, "rollback-target", "ubuntu")
	vm.Spec.Disks = []platformv1alpha1.DiskAttachment{{Claim: "payload"}}
	vm.Spec.Running = true
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the disk to be attached", func() error {
		got := getVM(t, ns, "rollback-target")
		if len(got.Status.AttachedDisks) != 1 {
			return fmt.Errorf("attachedDisks = %v", got.Status.AttachedDisks)
		}
		return nil
	})

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "roll-it-back", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:       "rollback-target",
			Action:       platformv1alpha1.OperationRollbackDisk,
			RollbackDisk: &platformv1alpha1.RollbackDiskSpec{SnapshotName: "payload-snap"},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	// The replacement is built before anything is taken away, and the original
	// is still there while it builds.
	eventually(t, "the replacement disk to be built from the snapshot", func() error {
		got := getOp(t, ns, "roll-it-back")
		if got.Status.ReplacementDisk == "" {
			return fmt.Errorf("no replacement named yet")
		}
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: got.Status.ReplacementDisk,
		}, dv); err != nil {
			return err
		}
		if dv.Spec.Source == nil || dv.Spec.Source.Snapshot == nil ||
			dv.Spec.Source.Snapshot.Name != "payload-snap" {
			return fmt.Errorf("the replacement is not built from the snapshot")
		}
		original := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "payload"}, original); err != nil {
			return fmt.Errorf("the original disk was removed before its replacement was ready: %w", err)
		}
		return nil
	})

	// CDI finishes building it.
	replacementName := getOp(t, ns, "roll-it-back").Status.ReplacementDisk
	eventually(t, "the replacement to report itself complete", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: replacementName}, dv); err != nil {
			return err
		}
		dv.Status.Phase = cdiv1.Succeeded
		return k8sClient.Status().Update(testCtx, dv)
	})

	// The machine has to come down before the swap; envtest has no
	// virt-controller, so nothing is running and it is down already.
	eventually(t, "the machine to be pointed at the replacement", func() error {
		got := getVM(t, ns, "rollback-target")
		if len(got.Spec.Disks) != 1 {
			return fmt.Errorf("disks = %v", got.Spec.Disks)
		}
		if got.Spec.Disks[0].Claim != replacementName {
			return fmt.Errorf("still pointed at %q", got.Spec.Disks[0].Claim)
		}
		return nil
	})

	eventually(t, "the operation to finish and the old disk to be gone", func() error {
		got := getOp(t, ns, "roll-it-back")
		if got.Status.Phase != platformv1alpha1.OperationPhaseSucceeded {
			return fmt.Errorf("phase = %q (%s)", got.Status.Phase, got.Status.Message)
		}
		old := &cdiv1.DataVolume{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "payload"}, old)
		if err == nil {
			return fmt.Errorf("the replaced disk is still there")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	// And the machine is running again, because it was running before.
	eventually(t, "the declared power state to be put back", func() error {
		got := getVM(t, ns, "rollback-target")
		if !got.Spec.Running {
			return fmt.Errorf("the machine was left stopped")
		}
		return nil
	})
}

// A snapshot of a disk this machine has never had is refused, and the refusal
// says what it looked at.
//
// This test used to assert the other half of that sentence — that a machine's
// own root disk could not be rolled back either, with a refusal pointing at
// VirtualMachineSnapshot and Restore. That refusal is gone, because the thing
// it refused now works: Recreate already replaces a root disk safely, and a
// rollback differs from it only in where the replacement's contents come from.
// TestRollingBackTheMachinesOwnDisk measures that path.
//
// Keeping this test as it was would have protected the limitation rather than
// the meaning. What is still true, and what it checks now, is that a snapshot
// belonging to neither an attached disk nor this machine's own is not
// something to guess about.
func TestRollingBackADiskThisMachineDoesNotHaveIsRefused(t *testing.T) {
	ns := "op-rollback-root"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "rooted", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the machine to exist", func() error {
		_, err := getKubeVirtVM(ns, "rooted")
		return err
	})
	// A snapshot of somebody else's claim entirely.
	mustVolumeSnapshot(t, ns, "stranger-snap", "some-other-disk", "20Gi", true)

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "roll-stranger", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:       "rooted",
			Action:       platformv1alpha1.OperationRollbackDisk,
			RollbackDisk: &platformv1alpha1.RollbackDiskSpec{SnapshotName: "stranger-snap"},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	eventually(t, "the refusal to name the disk it could not place", func() error {
		got := getOp(t, ns, "roll-stranger")
		if got.Status.Phase != platformv1alpha1.OperationPhaseFailed {
			return fmt.Errorf("phase = %q (%s)", got.Status.Phase, got.Status.Message)
		}
		if !strings.Contains(got.Status.Message, "some-other-disk") {
			return fmt.Errorf("the refusal does not name it: %q", got.Status.Message)
		}
		return nil
	})
}

// Recreating means wiping the machine back to its image — destructive by
// intent. What it must not be is destructive by accident: the machine is
// pointed at a fresh disk before the one it was using is removed, and the fresh
// disk gets a new name so it cannot collide with a predecessor that is still
// terminating.
func TestRecreatePointsAtAFreshDiskBeforeRemovingTheOldOne(t *testing.T) {
	ns := "op-recreate"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	vm := newManagedVM(ns, "wipe-me", "ubuntu")
	vm.Spec.Running = true
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the machine and its first disk to exist", func() error {
		kvm, err := getKubeVirtVM(ns, "wipe-me")
		if err != nil {
			return err
		}
		if kvm.Spec.DataVolumeTemplates[0].Name != "wipe-me-root-1" {
			return fmt.Errorf("first disk = %q", kvm.Spec.DataVolumeTemplates[0].Name)
		}
		return nil
	})

	// KubeVirt would build this from the template; envtest has no CDI.
	firstDisk := &cdiv1.DataVolume{
		ObjectMeta: metav1.ObjectMeta{Name: "wipe-me-root-1", Namespace: ns},
		Spec: cdiv1.DataVolumeSpec{
			Source:  &cdiv1.DataVolumeSource{Blank: &cdiv1.DataVolumeBlankImage{}},
			Storage: &cdiv1.StorageSpec{},
		},
	}
	if err := k8sClient.Create(testCtx, firstDisk); err != nil {
		t.Fatalf("creating the first disk: %v", err)
	}

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "start-over", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:   "wipe-me",
			Action:   platformv1alpha1.OperationRecreate,
			Recreate: &platformv1alpha1.RecreateSpec{},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	eventually(t, "the machine to be pointed at a disk with the next epoch", func() error {
		kvm, err := getKubeVirtVM(ns, "wipe-me")
		if err != nil {
			return err
		}
		if kvm.Spec.DataVolumeTemplates[0].Name != "wipe-me-root-2" {
			return fmt.Errorf("template disk = %q, want the next epoch",
				kvm.Spec.DataVolumeTemplates[0].Name)
		}
		for _, v := range kvm.Spec.Template.Spec.Volumes {
			if v.Name == "rootdisk" {
				if v.DataVolume == nil || v.DataVolume.Name != "wipe-me-root-2" {
					return fmt.Errorf("the root volume still points at the old disk")
				}
			}
		}
		return nil
	})

	eventually(t, "the operation to finish and the old disk to be gone", func() error {
		got := getOp(t, ns, "start-over")
		if got.Status.Phase != platformv1alpha1.OperationPhaseSucceeded {
			return fmt.Errorf("phase = %q (%s)", got.Status.Phase, got.Status.Message)
		}
		old := &cdiv1.DataVolume{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "wipe-me-root-1"}, old)
		if err == nil {
			return fmt.Errorf("the old disk is still there")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	eventually(t, "the epoch to be recorded and the power state restored", func() error {
		got := getVM(t, ns, "wipe-me")
		if got.Status.RootDiskEpoch != 2 {
			return fmt.Errorf("rootDiskEpoch = %d", got.Status.RootDiskEpoch)
		}
		if !got.Spec.Running {
			return fmt.Errorf("the machine was left stopped")
		}
		return nil
	})
}

// Cloning delegates to KubeVirt, which handles volume naming, fresh MAC
// addresses and a fresh SMBIOS serial. The path this replaces built the copy by
// hand with a rename map covering only dataVolumeTemplates, so an *attached*
// claim was copied verbatim and the clone referenced the source's own disks.
func TestCloningDelegatesAndBringsTheCopyUnderManagement(t *testing.T) {
	ns := "op-clone"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustDataVolume(t, ns, "not-to-be-copied")

	vm := newManagedVM(ns, "original", "ubuntu")
	vm.Spec.Disks = []platformv1alpha1.DiskAttachment{{Claim: "not-to-be-copied"}}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the machine to exist", func() error {
		_, err := getKubeVirtVM(ns, "original")
		return err
	})

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "copy-it", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName: "original",
			Action: platformv1alpha1.OperationClone,
			Clone:  &platformv1alpha1.CloneSpec{TargetName: "the-copy"},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	eventually(t, "KubeVirt to be asked, rather than the copy being hand-built", func() error {
		got := getOp(t, ns, "copy-it")
		if got.Status.ChildName == "" {
			return fmt.Errorf("no clone object yet")
		}
		clone := &clonev1beta1.VirtualMachineClone{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: got.Status.ChildName,
		}, clone); err != nil {
			return err
		}
		if clone.Spec.Source == nil || clone.Spec.Source.Name != "original" {
			return fmt.Errorf("clone source = %+v", clone.Spec.Source)
		}
		if clone.Spec.Target == nil || clone.Spec.Target.Name != "the-copy" {
			return fmt.Errorf("clone target = %+v", clone.Spec.Target)
		}
		return nil
	})

	// KubeVirt reports it done and names what it produced.
	eventually(t, "the clone to be marked finished", func() error {
		clone := &clonev1beta1.VirtualMachineClone{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: getOp(t, ns, "copy-it").Status.ChildName,
		}, clone); err != nil {
			return err
		}
		target := "the-copy"
		clone.Status.TargetName = &target
		clone.Status.Phase = clonev1beta1.Succeeded
		return k8sClient.Status().Update(testCtx, clone)
	})

	eventually(t, "the copy to be described and adopted, not rendered over", func() error {
		got := getOp(t, ns, "copy-it")
		if got.Status.Phase != platformv1alpha1.OperationPhaseSucceeded {
			return fmt.Errorf("phase = %q (%s)", got.Status.Phase, got.Status.Message)
		}
		copyVM := &platformv1alpha1.ManagedVM{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: "the-copy",
		}, copyVM); err != nil {
			return err
		}
		if copyVM.Annotations[naming.AdoptAnnotation] != "the-copy" {
			return fmt.Errorf("the copy is not marked for adoption: %v", copyVM.Annotations)
		}
		// The source's attached disk belongs to the source. A copy pointing at
		// it would be two machines on one filesystem — the defect being removed.
		if len(copyVM.Spec.Disks) != 0 {
			return fmt.Errorf("the copy claims the original's disks: %v", copyVM.Spec.Disks)
		}
		return nil
	})
}
