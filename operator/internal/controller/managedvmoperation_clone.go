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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	clonev1beta1 "kubevirt.io/api/clone/v1beta1"
	kubevirtcore "kubevirt.io/api/core"
	ctrl "sigs.k8s.io/controller-runtime"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

// reconcileClone copies a machine using KubeVirt's own clone controller, and
// then brings the copy under management.
//
// It delegates rather than reimplements, and that is the point. The path this
// replaces built the copy by hand with a rename map that covered only
// dataVolumeTemplates: any *attached* claim was copied across verbatim, so the
// clone referenced the source's own disks — two machines on one filesystem,
// created deliberately, by the feature meant to give you a separate one.
// Upstream handles volume naming, fresh MAC addresses and a fresh SMBIOS
// serial, and has a state machine of its own.
func (r *ManagedVMOperationReconciler) reconcileClone(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	vm *platformv1alpha1.ManagedVM,
) (ctrl.Result, error) {
	source := vm.Status.VirtualMachineName
	if source == "" {
		source = vm.Name
	}

	childName := op.Status.ChildName
	if childName == "" {
		childName = fmt.Sprintf("%s-clone", op.Name)
	}

	clone := &clonev1beta1.VirtualMachineClone{}
	err := r.Get(ctx, types.NamespacedName{Namespace: op.Namespace, Name: childName}, clone)
	switch {
	case apierrors.IsNotFound(err):
		created := &clonev1beta1.VirtualMachineClone{
			ObjectMeta: metav1.ObjectMeta{
				Name:      childName,
				Namespace: op.Namespace,
				Labels: map[string]string{
					naming.OwnerKindLabel: "ManagedVMOperation",
					naming.OwnerNameLabel: op.Name,
					naming.OwnerUIDLabel:  string(op.UID),
				},
			},
			Spec: clonev1beta1.VirtualMachineCloneSpec{
				Source: &corev1.TypedLocalObjectReference{
					APIGroup: ptrString(kubevirtcore.GroupName),
					Kind:     "VirtualMachine",
					Name:     source,
				},
			},
		}
		if target := op.Spec.Clone.TargetName; target != "" {
			created.Spec.Target = &corev1.TypedLocalObjectReference{
				APIGroup: ptrString(kubevirtcore.GroupName),
				Kind:     "VirtualMachine",
				Name:     target,
			}
		}
		if err := r.Create(ctx, created); err != nil {
			return ctrl.Result{}, fmt.Errorf("asking KubeVirt to clone %s: %w", source, err)
		}
		kube.CountWrite(r.Scheme, created, operationControllerName, "created")
		op.Status.ChildName = childName
		r.running(op, fmt.Sprintf("cloning %s", source))
		return ctrl.Result{RequeueAfter: operationPoll}, nil

	case err != nil:
		return ctrl.Result{}, fmt.Errorf("reading the clone: %w", err)
	}

	op.Status.ChildName = childName
	if clone.Status.TargetName != nil {
		op.Status.TargetName = *clone.Status.TargetName
	}

	switch clone.Status.Phase {
	case clonev1beta1.Succeeded:
		if op.Status.TargetName == "" {
			r.finish(op, platformv1alpha1.OperationPhaseFailed,
				"the clone finished without naming what it produced")
			return ctrl.Result{}, nil
		}
		if err := r.adoptClone(ctx, op, vm); err != nil {
			return ctrl.Result{}, err
		}
		r.finish(op, platformv1alpha1.OperationPhaseSucceeded,
			fmt.Sprintf("cloned %s to %s", source, op.Status.TargetName))
	case clonev1beta1.Failed:
		r.finish(op, platformv1alpha1.OperationPhaseFailed,
			fmt.Sprintf("KubeVirt could not clone %s", source))
	default:
		r.running(op, fmt.Sprintf("cloning %s (%s)", source, orUnknown(string(clone.Status.Phase))))
		return ctrl.Result{RequeueAfter: operationPoll}, nil
	}
	return ctrl.Result{}, nil
}

// adoptClone gives the copy the same description as the machine it came from.
//
// Without this the clone is a KubeVirt object nobody manages: invisible to
// everything that reads our resources, and outside every rule the description
// carries. It adopts rather than renders, because the machine already exists —
// KubeVirt made it — and rendering over it would fight whatever upstream chose.
func (r *ManagedVMOperationReconciler) adoptClone(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	source *platformv1alpha1.ManagedVM,
) error {
	adopted := &platformv1alpha1.ManagedVM{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: op.Namespace, Name: op.Status.TargetName,
	}, adopted)
	if err == nil {
		return nil
	}
	if !apierrors.IsNotFound(err) {
		return fmt.Errorf("reading the copy's description: %w", err)
	}

	spec := *source.Spec.DeepCopy()
	spec.DisplayName = fmt.Sprintf("%s (copy)", displayNameOf(source))
	spec.Running = op.Spec.Clone.StartAfterClone
	// Attached disks are not copied: they belong to the machine that has them,
	// and a copy pointing at them would be the very thing this replaces.
	spec.Disks = nil
	// The first-boot password belonged to the original's creation, not to a
	// copy made later.
	spec.InitialPasswordSecretRef = nil

	copyVM := &platformv1alpha1.ManagedVM{
		ObjectMeta: metav1.ObjectMeta{
			Name:      op.Status.TargetName,
			Namespace: op.Namespace,
			Annotations: map[string]string{
				naming.AdoptAnnotation: op.Status.TargetName,
			},
		},
		Spec: spec,
	}
	if owner := source.Annotations["kubevirt-ui.io/owner"]; owner != "" {
		copyVM.Annotations["kubevirt-ui.io/owner"] = owner
	}

	if err := r.Create(ctx, copyVM); err != nil && !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("describing the copy: %w", err)
	}
	kube.CountWrite(r.Scheme, copyVM, operationControllerName, "created")
	return nil
}

func displayNameOf(vm *platformv1alpha1.ManagedVM) string {
	if vm.Spec.DisplayName != "" {
		return vm.Spec.DisplayName
	}
	return vm.Name
}
