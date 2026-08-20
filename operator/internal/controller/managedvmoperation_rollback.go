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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	kubevirtv1 "kubevirt.io/api/core/v1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

var volumeSnapshotGVK = schema.GroupVersionKind{
	Group: "snapshot.storage.k8s.io", Version: "v1", Kind: "VolumeSnapshot",
}

// reconcileRollbackDisk puts one attached disk back to a snapshot by replacing
// it, never by destroying it first.
//
// The order is the whole design. Build the replacement, point the machine at
// it, then remove what it replaced. A pass that dies at any step leaves the
// machine with a disk — the old one if the swap has not happened, the new one
// if it has. The path this replaces deleted the claim and created its
// successor afterwards, so a process that died in between left a machine with
// no disk and no record of what it should have had.
func (r *ManagedVMOperationReconciler) reconcileRollbackDisk(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	vm *platformv1alpha1.ManagedVM,
) (ctrl.Result, error) {
	snapshot := &unstructured.Unstructured{}
	snapshot.SetGroupVersionKind(volumeSnapshotGVK)
	err := r.Get(ctx, types.NamespacedName{
		Namespace: op.Namespace, Name: op.Spec.RollbackDisk.SnapshotName,
	}, snapshot)
	if err != nil {
		if apierrors.IsNotFound(err) {
			r.finish(op, platformv1alpha1.OperationPhaseFailed, fmt.Sprintf(
				"VolumeSnapshot %s/%s does not exist",
				op.Namespace, op.Spec.RollbackDisk.SnapshotName))
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("reading the snapshot: %w", err)
	}

	ready, _, _ := unstructured.NestedBool(snapshot.Object, "status", "readyToUse")
	if !ready {
		r.running(op, "waiting for the snapshot to be usable")
		return ctrl.Result{RequeueAfter: operationPoll}, nil
	}

	source, _, _ := unstructured.NestedString(
		snapshot.Object, "spec", "source", "persistentVolumeClaimName")
	if source == "" {
		r.finish(op, platformv1alpha1.OperationPhaseFailed,
			"the snapshot does not name the claim it was taken from")
		return ctrl.Result{}, nil
	}

	// The machine's own root disk is not one of these. Rolling it back means
	// replacing what the machine was built from, which is what a VM snapshot
	// and a Restore are for; doing it through a claim swap would leave the
	// machine's own template describing a disk that no longer exists.
	attached := false
	for _, disk := range vm.Spec.Disks {
		if disk.Claim == source {
			attached = true
			break
		}
	}
	if !attached {
		r.finish(op, platformv1alpha1.OperationPhaseFailed, fmt.Sprintf(
			"%s is not one of this machine's attached disks; to put a machine's own "+
				"root disk back, take a VirtualMachineSnapshot and use a Restore",
			source))
		return ctrl.Result{}, nil
	}

	if op.Status.ReplacedDisk == "" {
		op.Status.ReplacedDisk = source
	}
	if op.Status.ReplacementDisk == "" {
		op.Status.ReplacementDisk = fmt.Sprintf("%s-rb-%s", source, op.Name[max(0, len(op.Name)-6):])
	}

	replacement := &cdiv1.DataVolume{}
	err = r.Get(ctx, types.NamespacedName{
		Namespace: op.Namespace, Name: op.Status.ReplacementDisk,
	}, replacement)
	switch {
	case apierrors.IsNotFound(err):
		built, buildErr := r.buildReplacementDisk(ctx, op, snapshot, source)
		if buildErr != nil {
			return ctrl.Result{}, buildErr
		}
		if err := r.Create(ctx, built); err != nil && !apierrors.IsAlreadyExists(err) {
			return ctrl.Result{}, fmt.Errorf("building the replacement disk: %w", err)
		}
		kube.CountWrite(r.Scheme, built, operationControllerName, "created")
		r.running(op, fmt.Sprintf("building %s from %s",
			op.Status.ReplacementDisk, op.Spec.RollbackDisk.SnapshotName))
		return ctrl.Result{RequeueAfter: operationPoll}, nil
	case err != nil:
		return ctrl.Result{}, fmt.Errorf("reading the replacement disk: %w", err)
	}

	if replacement.Status.Phase != cdiv1.Succeeded {
		r.running(op, fmt.Sprintf("restoring %s into %s (%s)",
			op.Spec.RollbackDisk.SnapshotName, op.Status.ReplacementDisk,
			orUnknown(string(replacement.Status.Phase))))
		return ctrl.Result{RequeueAfter: operationPoll}, nil
	}

	// The machine has to be down for the swap: the guest has the old disk
	// mounted, and pulling it out from under a running filesystem is how a
	// rollback turns into a corruption.
	stopped, err := r.ensureStopped(ctx, vm)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !stopped {
		r.running(op, "waiting for the machine to stop before swapping the disk")
		return ctrl.Result{RequeueAfter: operationPoll}, nil
	}

	if err := r.swapDisk(ctx, vm, op.Status.ReplacedDisk, op.Status.ReplacementDisk); err != nil {
		return ctrl.Result{}, err
	}

	// Only now is the disk it replaced removed. Up to this point every failure
	// leaves the machine on the disk it started with.
	old := &cdiv1.DataVolume{}
	err = r.Get(ctx, types.NamespacedName{Namespace: op.Namespace, Name: op.Status.ReplacedDisk}, old)
	switch {
	case err == nil:
		if err := kube.Delete(ctx, r.Client, operationControllerName, old); err != nil {
			return ctrl.Result{}, fmt.Errorf("removing the replaced disk: %w", err)
		}
	case !apierrors.IsNotFound(err):
		return ctrl.Result{}, fmt.Errorf("reading the replaced disk: %w", err)
	}

	if err := r.restoreRunState(ctx, op, vm); err != nil {
		return ctrl.Result{}, err
	}
	r.finish(op, platformv1alpha1.OperationPhaseSucceeded, fmt.Sprintf(
		"%s rolled back to %s as %s",
		op.Status.ReplacedDisk, op.Spec.RollbackDisk.SnapshotName, op.Status.ReplacementDisk))
	return ctrl.Result{}, nil
}

// buildReplacementDisk renders a disk restored from the snapshot, carrying the
// labels of the one it replaces so the product still recognises it.
func (r *ManagedVMOperationReconciler) buildReplacementDisk(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	snapshot *unstructured.Unstructured,
	source string,
) (*cdiv1.DataVolume, error) {
	size := "1Gi"
	if restoreSize, found, _ := unstructured.NestedString(
		snapshot.Object, "status", "restoreSize"); found && restoreSize != "" {
		size = restoreSize
	}

	labels := map[string]string{naming.ManagedLabel: "true"}
	storageClass := ""
	existing := &cdiv1.DataVolume{}
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: op.Namespace, Name: source,
	}, existing); err == nil {
		for k, v := range existing.Labels {
			// The holder label belongs to the disk that is attached now; the
			// replacement gets it when the machine is pointed at it.
			if k == attachedToLabel || k == attachedToUIDLabel {
				continue
			}
			labels[k] = v
		}
		if existing.Spec.Storage != nil {
			if existing.Spec.Storage.StorageClassName != nil {
				storageClass = *existing.Spec.Storage.StorageClassName
			}
			if req, ok := existing.Spec.Storage.Resources.Requests[corev1.ResourceStorage]; ok {
				size = req.String()
			}
		}
	} else if !apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("reading the disk being replaced: %w", err)
	}

	quantity, err := resource.ParseQuantity(size)
	if err != nil {
		return nil, fmt.Errorf("snapshot restore size %q is not a quantity: %w", size, err)
	}

	storage := &cdiv1.StorageSpec{
		Resources: corev1.VolumeResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceStorage: quantity},
		},
	}
	if storageClass != "" {
		sc := storageClass
		storage.StorageClassName = &sc
	}

	return &cdiv1.DataVolume{
		ObjectMeta: metav1.ObjectMeta{
			Name:      op.Status.ReplacementDisk,
			Namespace: op.Namespace,
			Labels:    labels,
		},
		Spec: cdiv1.DataVolumeSpec{
			Source: &cdiv1.DataVolumeSource{
				Snapshot: &cdiv1.DataVolumeSourceSnapshot{
					Namespace: op.Namespace,
					Name:      op.Spec.RollbackDisk.SnapshotName,
				},
			},
			Storage: storage,
		},
	}, nil
}

// ensureStopped halts the machine and reports whether it is down yet.
//
// It writes runStrategy on the machine directly rather than through the
// resource, because the VM controller is standing aside for the duration of
// this operation and would not act on it. The declared state is put back at the
// end from what was recorded when the operation began.
func (r *ManagedVMOperationReconciler) ensureStopped(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) (bool, error) {
	name := vm.Status.VirtualMachineName
	if name == "" {
		name = vm.Name
	}

	kvm := &kubevirtv1.VirtualMachine{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: vm.Namespace, Name: name}, kvm); err != nil {
		if apierrors.IsNotFound(err) {
			return true, nil
		}
		return false, fmt.Errorf("reading the machine: %w", err)
	}
	if kvm.Spec.RunStrategy == nil || *kvm.Spec.RunStrategy != kubevirtv1.RunStrategyHalted {
		patched := kvm.DeepCopy()
		halted := kubevirtv1.RunStrategyHalted
		patched.Spec.RunStrategy = &halted
		if err := r.Update(ctx, patched); err != nil {
			return false, fmt.Errorf("stopping the machine: %w", err)
		}
		kube.CountWrite(r.Scheme, patched, operationControllerName, "updated")
		return false, nil
	}

	vmi := &kubevirtv1.VirtualMachineInstance{}
	err := r.Get(ctx, types.NamespacedName{Namespace: vm.Namespace, Name: name}, vmi)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, fmt.Errorf("reading the running instance: %w", err)
	}
	return false, nil
}

// swapDisk points the machine's declared disk list at the replacement.
func (r *ManagedVMOperationReconciler) swapDisk(
	ctx context.Context, vm *platformv1alpha1.ManagedVM, from, to string,
) error {
	patched := vm.DeepCopy()
	changed := false
	for i := range patched.Spec.Disks {
		if patched.Spec.Disks[i].Claim == from {
			patched.Spec.Disks[i].Claim = to
			changed = true
		}
	}
	if !changed {
		return nil
	}
	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("pointing the machine at the replacement disk: %w", err)
	}
	kube.CountWrite(r.Scheme, patched, operationControllerName, "updated")
	*vm = *patched
	return nil
}
