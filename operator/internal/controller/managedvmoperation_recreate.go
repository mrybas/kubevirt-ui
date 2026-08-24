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
	"strconv"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"
	ctrl "sigs.k8s.io/controller-runtime"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

// reconcileRecreate wipes a machine back to the image it was built from.
//
// Destructive by intent, but not by accident. The machine's own template is
// pointed at a fresh disk name first, so KubeVirt builds a new disk from the
// image, and only then is the disk it was using removed. The path this replaces
// deleted the disk and relied on KubeVirt to notice and rebuild it under the
// same name — which leaves a window where the machine's disk simply does not
// exist, and a name that has to be reused while its predecessor may still be
// terminating.
//
// This is one of the two places allowed to rewrite the machine's own volume
// arrays. It is safe here for the same reason the rule exists: the VM
// controller stands aside for the whole operation, so there is exactly one
// writer.
func (r *ManagedVMOperationReconciler) reconcileRecreate(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	vm *platformv1alpha1.ManagedVM,
) (ctrl.Result, error) {
	name := vm.Status.VirtualMachineName
	if name == "" {
		name = vm.Name
	}

	kvm := &kubevirtv1.VirtualMachine{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: op.Namespace, Name: name}, kvm); err != nil {
		if apierrors.IsNotFound(err) {
			r.finish(op, platformv1alpha1.OperationPhaseFailed,
				fmt.Sprintf("VirtualMachine %s/%s does not exist", op.Namespace, name))
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("reading the machine: %w", err)
	}
	if len(kvm.Spec.DataVolumeTemplates) == 0 {
		r.finish(op, platformv1alpha1.OperationPhaseFailed,
			"this machine has no disk of its own to recreate")
		return ctrl.Result{}, nil
	}

	current := kvm.Spec.DataVolumeTemplates[0].Name
	if op.Status.ReplacedDisk == "" {
		op.Status.ReplacedDisk = current
	}
	if op.Status.ReplacementDisk == "" {
		op.Status.ReplacementDisk = nextRootDiskName(vm.Name, current)
	}

	// The machine has to be down: the disk it is running on is about to stop
	// being its disk.
	stopped, err := r.ensureStopped(ctx, vm)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !stopped {
		r.running(op, "waiting for the machine to stop before rebuilding its disk")
		return ctrl.Result{RequeueAfter: operationPoll}, nil
	}

	swapped, err := r.pointAtRootDisk(ctx, op, vm, kvm, current, "rebuilding")
	if err != nil {
		return ctrl.Result{}, err
	}
	if !swapped {
		return ctrl.Result{RequeueAfter: operationPoll}, nil
	}

	if err := r.restoreRunState(ctx, op, vm); err != nil {
		return ctrl.Result{}, err
	}
	r.finish(op, platformv1alpha1.OperationPhaseSucceeded, fmt.Sprintf(
		"%s rebuilt from its image as %s", vm.Name, op.Status.ReplacementDisk))
	return ctrl.Result{}, nil
}

// pointAtRootDisk moves a machine onto another root disk and retires the one
// it was on: rewrite the template and the volume, then — on the next pass,
// when nothing references it any more — delete the predecessor and record the
// epoch.
//
// Shared by Recreate and by a rollback of the root disk, which differ only in
// where the replacement's contents come from. They had one implementation and
// one comment between them before the rollback needed it too; two would be two
// answers to "what is this machine's disk".
//
// Returns false while the swap is still in flight, so the caller requeues.
//
// The order carries the whole safety property. The claim is deleted only after
// nothing mounts it and the machine is stopped — a claim deleted while a guest
// has it mounted does not go away, it sits in Terminating behind
// pvc-protection with the pod still writing to it, and takes the operation
// with it.
func (r *ManagedVMOperationReconciler) pointAtRootDisk(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	vm *platformv1alpha1.ManagedVM,
	kvm *kubevirtv1.VirtualMachine,
	current string,
	verb string,
) (bool, error) {
	if current != op.Status.ReplacementDisk {
		patched := kvm.DeepCopy()
		patched.Spec.DataVolumeTemplates[0].Name = op.Status.ReplacementDisk
		for i := range patched.Spec.Template.Spec.Volumes {
			vol := &patched.Spec.Template.Spec.Volumes[i]
			if vol.DataVolume != nil && vol.DataVolume.Name == op.Status.ReplacedDisk {
				vol.DataVolume.Name = op.Status.ReplacementDisk
			}
		}
		if err := r.Update(ctx, patched); err != nil {
			return false, fmt.Errorf("pointing the machine at a fresh disk: %w", err)
		}
		kube.CountWrite(r.Scheme, patched, operationControllerName, "updated")
		r.running(op, fmt.Sprintf("%s %s as %s",
			verb, op.Status.ReplacedDisk, op.Status.ReplacementDisk))
		return false, nil
	}

	// Nothing references the old disk any more, so removing it cannot leave the
	// machine without one.
	if op.Status.ReplacedDisk != op.Status.ReplacementDisk {
		old := &cdiv1.DataVolume{}
		err := r.Get(ctx, types.NamespacedName{
			Namespace: op.Namespace, Name: op.Status.ReplacedDisk,
		}, old)
		switch {
		case err == nil:
			if err := kube.Delete(ctx, r.Client, operationControllerName, old); err != nil {
				return false, fmt.Errorf("removing the old disk: %w", err)
			}
		case !apierrors.IsNotFound(err):
			return false, fmt.Errorf("reading the old disk: %w", err)
		}
	}

	// Record the epoch on the machine so the next rebuild picks the next name
	// rather than reusing one that may still be terminating.
	if epoch := epochOf(op.Status.ReplacementDisk); epoch > vm.Status.RootDiskEpoch {
		patched := vm.DeepCopy()
		patched.Status.RootDiskEpoch = epoch
		patched.Status.RootDiskName = op.Status.ReplacementDisk
		if err := r.Status().Update(ctx, patched); err != nil {
			return false, fmt.Errorf("recording the disk epoch: %w", err)
		}
		kube.CountWrite(r.Scheme, patched, operationControllerName, "status")
		*vm = *patched
	}
	return true, nil
}

// nextRootDiskName advances the epoch in a root disk's name.
//
// A fresh disk gets a fresh name so it cannot collide with a predecessor that
// is still terminating — the failure the old path could hit whenever a rebuild
// followed too closely on the last one.
func nextRootDiskName(vmName, current string) string {
	prefix := vmName + "-root-"
	if strings.HasPrefix(current, prefix) {
		if epoch, err := strconv.Atoi(strings.TrimPrefix(current, prefix)); err == nil {
			return fmt.Sprintf("%s%d", prefix, epoch+1)
		}
	}
	// A disk named by something else — a restore, or a machine that predates
	// this naming. Start the sequence rather than guessing at its scheme.
	return prefix + "2"
}

func epochOf(diskName string) int32 {
	idx := strings.LastIndex(diskName, "-root-")
	if idx < 0 {
		return 0
	}
	epoch, err := strconv.Atoi(diskName[idx+len("-root-"):])
	if err != nil {
		return 0
	}
	return int32(epoch)
}
