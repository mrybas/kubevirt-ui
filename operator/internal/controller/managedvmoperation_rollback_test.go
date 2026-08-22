package controller

import (
	"fmt"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// TestRollingBackTheMachinesOwnDisk.
//
// Reported from the stand: a rollback of a VM's root disk left the claim in
// Terminating behind pvc-protection, a finished clone with nowhere to go, and
// a VM the UI still showed as healthy while its disk was being deleted under
// it. Three things had to line up.
//
// The backend's owner lookup only scanned `spec.disks`, and a root disk is not
// there, so the request took the legacy path. That path stops a machine by
// patching `runStrategy` — a field this operator owns and writes straight back
// — so the machine never stopped; the two-minute wait expired and the code
// went on to delete the claim anyway.
//
// And had the operation been created, it would have refused: rolling back the
// root disk was "take a VirtualMachineSnapshot and use a Restore". Recreate
// already replaces a root disk properly — a fresh disk under the next epoch,
// the template pointed at it, the predecessor retired only once nothing holds
// it — and a rollback differs only in where the contents come from. So it goes
// through the same code.
//
// Nothing is deleted while the machine runs, which is the property that makes
// the deadlock impossible rather than unlikely.
func TestRollingBackTheMachinesOwnDisk(t *testing.T) {
	ns := "op-root-rollback"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	vm := newManagedVM(ns, "rootback", "ubuntu")
	vm.Spec.Running = true
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	var rootDisk string
	eventually(t, "the machine to have a root disk", func() error {
		kvm := &kubevirtv1.VirtualMachine{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: "rootback",
		}, kvm); err != nil {
			return err
		}
		if len(kvm.Spec.DataVolumeTemplates) == 0 {
			return fmt.Errorf("no dataVolumeTemplates yet")
		}
		rootDisk = kvm.Spec.DataVolumeTemplates[0].Name
		return nil
	})

	// envtest has no virt-controller, so the DataVolume the template names is
	// never actually created. Stand in for it: the point of this test is what
	// happens to the machine's disk, and it has to exist to be left alone.
	mustDataVolume(t, ns, rootDisk)
	mustVolumeSnapshot(t, ns, "root-snap", rootDisk, "10Gi", true)

	op := &platformv1alpha1.ManagedVMOperation{
		ObjectMeta: metav1.ObjectMeta{Name: "roll-the-root", Namespace: ns},
		Spec: platformv1alpha1.ManagedVMOperationSpec{
			VMName:       "rootback",
			Action:       platformv1alpha1.OperationRollbackDisk,
			RollbackDisk: &platformv1alpha1.RollbackDiskSpec{SnapshotName: "root-snap"},
		},
	}
	if err := k8sClient.Create(testCtx, op); err != nil {
		t.Fatalf("creating operation: %v", err)
	}

	// It is no longer refused, and the replacement is built before anything is
	// taken away — under the next epoch's name, so it cannot collide with a
	// predecessor that may still be terminating.
	var replacement string
	eventually(t, "the replacement root disk to be built from the snapshot", func() error {
		got := getOp(t, ns, "roll-the-root")
		if got.Status.Phase == platformv1alpha1.OperationPhaseFailed {
			return fmt.Errorf("refused: %s", got.Status.Message)
		}
		replacement = got.Status.ReplacementDisk
		if replacement == "" {
			return fmt.Errorf("no replacement named yet (%s)", got.Status.Message)
		}
		if !strings.HasPrefix(replacement, "rootback-root-") || replacement == rootDisk {
			return fmt.Errorf("replacement %q is not a fresh epoch of %q", replacement, rootDisk)
		}
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: replacement,
		}, dv); err != nil {
			return err
		}
		if dv.Spec.Source == nil || dv.Spec.Source.Snapshot == nil ||
			dv.Spec.Source.Snapshot.Name != "root-snap" {
			return fmt.Errorf("the replacement is not built from the snapshot")
		}
		// And the disk the machine is on is untouched while that happens.
		old := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: rootDisk,
		}, old); err != nil {
			return fmt.Errorf("the machine's disk went away before its replacement was ready: %w", err)
		}
		if old.DeletionTimestamp != nil {
			return fmt.Errorf("the machine's disk is being deleted while it is still its disk")
		}
		return nil
	})

	eventually(t, "the replacement to report itself complete", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: replacement,
		}, dv); err != nil {
			return err
		}
		dv.Status.Phase = cdiv1.Succeeded
		return k8sClient.Status().Update(testCtx, dv)
	})

	eventually(t, "the machine's template to name the replacement", func() error {
		kvm := &kubevirtv1.VirtualMachine{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: ns, Name: "rootback",
		}, kvm); err != nil {
			return err
		}
		if kvm.Spec.DataVolumeTemplates[0].Name != replacement {
			return fmt.Errorf("template still names %q", kvm.Spec.DataVolumeTemplates[0].Name)
		}
		// The volume too — a template pointing one way and a volume the other
		// is a machine that boots from the disk it was supposed to leave.
		for _, vol := range kvm.Spec.Template.Spec.Volumes {
			if vol.DataVolume != nil && vol.DataVolume.Name == rootDisk {
				return fmt.Errorf("a volume still names the old disk")
			}
		}
		return nil
	})

	eventually(t, "the operation to finish, and only then the old disk to go", func() error {
		got := getOp(t, ns, "roll-the-root")
		if got.Status.Phase != platformv1alpha1.OperationPhaseSucceeded {
			return fmt.Errorf("phase = %q (%s)", got.Status.Phase, got.Status.Message)
		}
		old := &cdiv1.DataVolume{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: rootDisk}, old)
		if err == nil {
			return fmt.Errorf("the replaced disk is still there")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		// The epoch is recorded, so the next replacement gets the next name
		// rather than one that may still be terminating.
		vm := getVM(t, ns, "rootback")
		if vm.Status.RootDiskName != replacement {
			return fmt.Errorf("status.rootDiskName = %q", vm.Status.RootDiskName)
		}
		return nil
	})
}

// TestASwapThatChangesNothingIsAnError.
//
// `swapDisk` returned nil when it found nothing to rewrite, and the caller
// went straight on to delete the disk it thought it had replaced. A rollback
// that quietly removes a disk it never swapped is the worst shape this code
// can take, so the silence is gone.
func TestASwapThatChangesNothingIsAnError(t *testing.T) {
	ns := "op-swap-noop"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	vm := newManagedVM(ns, "noswap", "ubuntu")
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	reconciler := &ManagedVMOperationReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
	err := reconciler.swapDisk(testCtx, getVM(t, ns, "noswap"), "not-attached", "replacement")
	if err == nil {
		t.Fatal("swapping a disk the machine does not have was reported as done")
	}
	if !strings.Contains(err.Error(), "not-attached") {
		t.Errorf("the error does not name the disk: %v", err)
	}
}
