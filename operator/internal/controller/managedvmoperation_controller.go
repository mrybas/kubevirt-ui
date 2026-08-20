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
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	kubevirtcore "kubevirt.io/api/core"
	kubevirtv1 "kubevirt.io/api/core/v1"
	snapshotv1beta1 "kubevirt.io/api/snapshot/v1beta1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

const (
	operationControllerName = "managedvmoperation"

	// operationPoll is the backstop between passes while an operation runs.
	// The children are watched, so this only covers what a watch cannot see.
	operationPoll = 10 * time.Second
)

// ManagedVMOperationReconciler runs one operation on one machine, in phases
// that live on the object.
//
// The point of the whole type is that it survives its own process. Every
// operation it replaces was a sequence of sleeps inside an HTTP handler: kill
// the process midway through a restore and the machine stayed stopped forever,
// because "it was running before" was a local variable.
type ManagedVMOperationReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedvmoperations,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedvmoperations/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=snapshot.kubevirt.io,resources=virtualmachinesnapshots,verbs=get;list;watch
// +kubebuilder:rbac:groups=snapshot.kubevirt.io,resources=virtualmachinerestores,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups=kubevirt.io,resources=virtualmachineinstancemigrations,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups=kubevirt.io,resources=virtualmachineinstances,verbs=get;list;watch

// Reconcile advances one operation by one step.
func (r *ManagedVMOperationReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	op := &platformv1alpha1.ManagedVMOperation{}
	if err := r.Get(ctx, req.NamespacedName, op); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !op.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	if op.Status.Finished() {
		return r.reapIfExpired(ctx, op)
	}

	before := op.DeepCopy()

	vm := &platformv1alpha1.ManagedVM{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: op.Namespace, Name: op.Spec.VMName}, vm); err != nil {
		if apierrors.IsNotFound(err) {
			// The machine may be created by the same apply. Waiting is the
			// right answer for a while; failing immediately would make the
			// order of a single apply part of the API.
			return r.wait(ctx, op, before, fmt.Sprintf(
				"ManagedVM %s/%s does not exist yet", op.Namespace, op.Spec.VMName))
		}
		return ctrl.Result{}, fmt.Errorf("reading ManagedVM: %w", err)
	}

	if op.Status.StartTime == nil {
		now := metav1.Now()
		op.Status.StartTime = &now
	}
	if op.Status.RunningBefore == nil {
		running := vm.Spec.Running
		op.Status.RunningBefore = &running
	}

	var (
		result ctrl.Result
		err    error
	)
	switch op.Spec.Action {
	case platformv1alpha1.OperationRestore:
		result, err = r.reconcileRestore(ctx, op, vm)
	case platformv1alpha1.OperationMigrate:
		result, err = r.reconcileMigrate(ctx, op, vm)
	default:
		r.finish(op, platformv1alpha1.OperationPhaseFailed,
			fmt.Sprintf("unknown action %q", op.Spec.Action))
	}
	if err != nil {
		return ctrl.Result{}, err
	}

	op.Status.ObservedGeneration = op.Generation
	if err := kube.UpdateStatus(ctx, r.Client, operationControllerName, op, before); err != nil {
		return ctrl.Result{}, fmt.Errorf("updating status: %w", err)
	}
	return result, nil
}

// reconcileRestore puts a machine back to a snapshot.
//
// KubeVirt's own restore controller does the work, including stopping the
// target: targetReadinessPolicy StopTarget. What is left for us is the part it
// does not do — remembering whether the machine had been running, and putting
// it back that way — and that is exactly the part the old code kept in a local
// variable.
func (r *ManagedVMOperationReconciler) reconcileRestore(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	vm *platformv1alpha1.ManagedVM,
) (ctrl.Result, error) {
	target := vm.Status.VirtualMachineName
	if target == "" {
		target = vm.Name
	}

	snapshot := &snapshotv1beta1.VirtualMachineSnapshot{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: op.Namespace, Name: op.Spec.Restore.SnapshotName,
	}, snapshot)
	if err != nil {
		if apierrors.IsNotFound(err) {
			r.finish(op, platformv1alpha1.OperationPhaseFailed, fmt.Sprintf(
				"snapshot %s/%s does not exist", op.Namespace, op.Spec.Restore.SnapshotName))
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("reading snapshot: %w", err)
	}

	childName := op.Status.ChildName
	if childName == "" {
		childName = fmt.Sprintf("%s-restore", op.Name)
	}

	restore := &snapshotv1beta1.VirtualMachineRestore{}
	err = r.Get(ctx, types.NamespacedName{Namespace: op.Namespace, Name: childName}, restore)
	switch {
	case apierrors.IsNotFound(err):
		policy := snapshotv1beta1.VirtualMachineRestoreStopTarget
		created := &snapshotv1beta1.VirtualMachineRestore{
			ObjectMeta: metav1.ObjectMeta{
				Name:      childName,
				Namespace: op.Namespace,
				Labels: map[string]string{
					naming.OwnerKindLabel: "ManagedVMOperation",
					naming.OwnerNameLabel: op.Name,
					naming.OwnerUIDLabel:  string(op.UID),
				},
			},
			Spec: snapshotv1beta1.VirtualMachineRestoreSpec{
				Target: corev1.TypedLocalObjectReference{
					APIGroup: ptrString(kubevirtcore.GroupName),
					Kind:     "VirtualMachine",
					Name:     target,
				},
				VirtualMachineSnapshotName: op.Spec.Restore.SnapshotName,
				// Let KubeVirt stop the machine. Doing it ourselves means a
				// stop, a poll, and a decision about what to do when the poll
				// times out — the old code's answer to which was to carry on
				// quietly and restore into a running machine.
				TargetReadinessPolicy: &policy,
			},
		}
		if err := r.Create(ctx, created); err != nil {
			return ctrl.Result{}, fmt.Errorf("creating VirtualMachineRestore: %w", err)
		}
		kube.CountWrite(r.Scheme, created, operationControllerName, "created")
		op.Status.ChildName = childName
		r.running(op, fmt.Sprintf("restoring %s from %s", target, op.Spec.Restore.SnapshotName))
		return ctrl.Result{RequeueAfter: operationPoll}, nil

	case err != nil:
		return ctrl.Result{}, fmt.Errorf("reading VirtualMachineRestore: %w", err)
	}

	op.Status.ChildName = childName

	if restore.Status != nil && restore.Status.Complete != nil && *restore.Status.Complete {
		// The machine was stopped to be restored. Put the declared state back
		// to what it was, which is the whole reason this is an object.
		if err := r.restoreRunState(ctx, op, vm); err != nil {
			return ctrl.Result{}, err
		}
		r.finish(op, platformv1alpha1.OperationPhaseSucceeded,
			fmt.Sprintf("restored %s from %s", target, op.Spec.Restore.SnapshotName))
		return ctrl.Result{}, nil
	}

	if restore.Status != nil {
		for _, cond := range restore.Status.Conditions {
			if cond.Type == snapshotv1beta1.ConditionFailure && cond.Status == corev1.ConditionTrue {
				// A failure is reported, not waited out. The old loop polled
				// for two minutes without ever looking for one, then returned
				// success.
				if err := r.restoreRunState(ctx, op, vm); err != nil {
					return ctrl.Result{}, err
				}
				msg := cond.Message
				if msg == "" {
					msg = cond.Reason
				}
				r.finish(op, platformv1alpha1.OperationPhaseFailed, "restore failed: "+msg)
				return ctrl.Result{}, nil
			}
		}
	}

	r.running(op, fmt.Sprintf("restoring %s from %s", target, op.Spec.Restore.SnapshotName))
	return ctrl.Result{RequeueAfter: operationPoll}, nil
}

// reconcileMigrate moves a running machine to another node.
//
// The target node becomes a selector on the migration object. The path this
// replaces put a nodeSelector on the machine itself and nothing ever took it
// off, so a VM migrated once was welded to that node for good — a defect that
// disappears here rather than being cleaned up, because the machine is never
// touched at all.
func (r *ManagedVMOperationReconciler) reconcileMigrate(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	vm *platformv1alpha1.ManagedVM,
) (ctrl.Result, error) {
	target := vm.Status.VirtualMachineName
	if target == "" {
		target = vm.Name
	}

	vmi := &kubevirtv1.VirtualMachineInstance{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: op.Namespace, Name: target}, vmi); err != nil {
		if apierrors.IsNotFound(err) {
			r.finish(op, platformv1alpha1.OperationPhaseFailed,
				fmt.Sprintf("%s is not running; there is nothing to migrate", target))
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("reading VirtualMachineInstance: %w", err)
	}

	if node := op.Spec.Migrate.TargetNode; node != "" && vmi.Status.NodeName == node {
		r.finish(op, platformv1alpha1.OperationPhaseSucceeded,
			fmt.Sprintf("%s is already on %s", target, node))
		return ctrl.Result{}, nil
	}

	childName := op.Status.ChildName
	if childName == "" {
		childName = fmt.Sprintf("%s-migration", op.Name)
	}

	migration := &kubevirtv1.VirtualMachineInstanceMigration{}
	err := r.Get(ctx, types.NamespacedName{Namespace: op.Namespace, Name: childName}, migration)
	switch {
	case apierrors.IsNotFound(err):
		created := &kubevirtv1.VirtualMachineInstanceMigration{
			ObjectMeta: metav1.ObjectMeta{
				Name:      childName,
				Namespace: op.Namespace,
				Labels: map[string]string{
					naming.OwnerKindLabel: "ManagedVMOperation",
					naming.OwnerNameLabel: op.Name,
					naming.OwnerUIDLabel:  string(op.UID),
				},
			},
			Spec: kubevirtv1.VirtualMachineInstanceMigrationSpec{VMIName: target},
		}
		if node := op.Spec.Migrate.TargetNode; node != "" {
			created.Spec.AddedNodeSelector = map[string]string{corev1.LabelHostname: node}
		}
		if err := r.Create(ctx, created); err != nil {
			return ctrl.Result{}, fmt.Errorf("creating VirtualMachineInstanceMigration: %w", err)
		}
		kube.CountWrite(r.Scheme, created, operationControllerName, "created")
		op.Status.ChildName = childName
		r.running(op, fmt.Sprintf("migrating %s", target))
		return ctrl.Result{RequeueAfter: operationPoll}, nil

	case err != nil:
		return ctrl.Result{}, fmt.Errorf("reading VirtualMachineInstanceMigration: %w", err)
	}

	op.Status.ChildName = childName

	switch migration.Status.Phase {
	case kubevirtv1.MigrationSucceeded:
		r.finish(op, platformv1alpha1.OperationPhaseSucceeded,
			fmt.Sprintf("%s now runs on %s", target, vmi.Status.NodeName))
	case kubevirtv1.MigrationFailed:
		r.finish(op, platformv1alpha1.OperationPhaseFailed,
			fmt.Sprintf("migration of %s failed", target))
	default:
		r.running(op, fmt.Sprintf("migrating %s (%s)", target, migration.Status.Phase))
		return ctrl.Result{RequeueAfter: operationPoll}, nil
	}
	return ctrl.Result{}, nil
}

// restoreRunState puts the declared power state back to what it was before.
func (r *ManagedVMOperationReconciler) restoreRunState(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	vm *platformv1alpha1.ManagedVM,
) error {
	if op.Status.RunningBefore == nil || vm.Spec.Running == *op.Status.RunningBefore {
		return nil
	}
	patched := vm.DeepCopy()
	patched.Spec.Running = *op.Status.RunningBefore
	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("restoring the declared power state: %w", err)
	}
	kube.CountWrite(r.Scheme, patched, operationControllerName, "updated")
	return nil
}

func (r *ManagedVMOperationReconciler) wait(
	ctx context.Context,
	op *platformv1alpha1.ManagedVMOperation,
	before *platformv1alpha1.ManagedVMOperation,
	message string,
) (ctrl.Result, error) {
	op.Status.Phase = platformv1alpha1.OperationPhasePending
	op.Status.Message = message
	if err := kube.UpdateStatus(ctx, r.Client, operationControllerName, op, before); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: operationPoll}, nil
}

func (r *ManagedVMOperationReconciler) running(op *platformv1alpha1.ManagedVMOperation, message string) {
	op.Status.Phase = platformv1alpha1.OperationPhaseRunning
	op.Status.Message = message
	apimeta.SetStatusCondition(&op.Status.Conditions, metav1.Condition{
		Type:               "Complete",
		Status:             metav1.ConditionFalse,
		Reason:             "InProgress",
		Message:            message,
		ObservedGeneration: op.Generation,
	})
}

func (r *ManagedVMOperationReconciler) finish(
	op *platformv1alpha1.ManagedVMOperation, phase, message string,
) {
	op.Status.Phase = phase
	op.Status.Message = message
	if op.Status.CompletionTime == nil {
		now := metav1.Now()
		op.Status.CompletionTime = &now
	}
	condStatus := metav1.ConditionTrue
	reason := "Succeeded"
	if phase == platformv1alpha1.OperationPhaseFailed {
		condStatus = metav1.ConditionFalse
		reason = "Failed"
	}
	apimeta.SetStatusCondition(&op.Status.Conditions, metav1.Condition{
		Type:               "Complete",
		Status:             condStatus,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: op.Generation,
	})
	if r.Recorder != nil {
		eventType := corev1.EventTypeNormal
		if phase == platformv1alpha1.OperationPhaseFailed {
			eventType = corev1.EventTypeWarning
		}
		r.Recorder.Event(op, eventType, phase, message)
	}
}

// reapIfExpired removes a finished operation once its time is up, so that a
// busy namespace does not silt up with history nobody reads.
func (r *ManagedVMOperationReconciler) reapIfExpired(
	ctx context.Context, op *platformv1alpha1.ManagedVMOperation,
) (ctrl.Result, error) {
	ttl := time.Duration(op.Spec.TTLSecondsAfterFinished) * time.Second
	if ttl == 0 || op.Status.CompletionTime == nil {
		return ctrl.Result{}, nil
	}
	expiry := op.Status.CompletionTime.Add(ttl)
	if remaining := time.Until(expiry); remaining > 0 {
		return ctrl.Result{RequeueAfter: remaining}, nil
	}
	if err := kube.Delete(ctx, r.Client, operationControllerName, op); err != nil {
		return ctrl.Result{}, fmt.Errorf("reaping finished operation: %w", err)
	}
	return ctrl.Result{}, nil
}

func ptrString(s string) *string { return &s }

// SetupWithManager wires the controller.
func (r *ManagedVMOperationReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.ManagedVMOperation{}).
		Watches(&snapshotv1beta1.VirtualMachineRestore{},
			handler.EnqueueRequestsFromMapFunc(mapOwnedToOperation)).
		Watches(&kubevirtv1.VirtualMachineInstanceMigration{},
			handler.EnqueueRequestsFromMapFunc(mapOwnedToOperation)).
		Named(operationControllerName).
		Complete(r)
}

func mapOwnedToOperation(_ context.Context, obj client.Object) []reconcile.Request {
	labels := obj.GetLabels()
	if labels[naming.OwnerKindLabel] != "ManagedVMOperation" {
		return nil
	}
	name := labels[naming.OwnerNameLabel]
	if name == "" {
		return nil
	}
	return []reconcile.Request{{
		NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: name},
	}}
}
