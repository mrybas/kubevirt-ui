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
	"encoding/json"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	kubevirtv1 "kubevirt.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/kubevirt"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

const (
	vmControllerName = "managedvm"

	// vmFinalizer makes deleting the description delete the machine.
	//
	// It is a finalizer rather than an ownerReference so that the cascade stays
	// this controller's decision: removing the CRD during a rollback must not
	// take live workloads with it, and the documented escape hatch — strip the
	// finalizers, then delete — leaves every machine running.
	vmFinalizer = "platform.kubevirt-ui.io/managedvm"

	// blockedVMRequeue is the backstop for states that clear themselves when
	// something else in the cluster changes. The watches are the mechanism;
	// this only covers what we do not watch.
	blockedVMRequeue = 30 * time.Second
)

// ManagedVMReconciler renders a ManagedVM into a KubeVirt VirtualMachine.
type ManagedVMReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder

	// KubeOVNNamespace is where kube-ovn's configuration lives. Read through a
	// function rather than captured at import time, so a change to it takes
	// effect on the next pass instead of at the next restart.
	KubeOVNNamespace func() string
}

func (r *ManagedVMReconciler) kubeOVNNamespaces() []string {
	if r.KubeOVNNamespace != nil {
		if ns := r.KubeOVNNamespace(); ns != "" {
			return []string{ns, "kube-ovn"}
		}
	}
	return []string{"kube-ovn"}
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedvms,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedvms/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedvms/finalizers,verbs=update
// +kubebuilder:rbac:groups=kubevirt.io,resources=virtualmachines,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=kubeovn.io,resources=subnets;vpcs,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch;delete

// Reconcile renders one ManagedVM.
func (r *ManagedVMReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	vm := &platformv1alpha1.ManagedVM{}
	if err := r.Get(ctx, req.NamespacedName, vm); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if vm.Annotations[pausedAnnotation] == "true" {
		log.Info("Paused by annotation, not reconciling")
		return ctrl.Result{}, nil
	}
	if !vm.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, vm)
	}

	before := vm.DeepCopy()

	// While an operation is running, the machine belongs to it. A restore stops
	// the machine and rewrites its disks; reconciling runStrategy underneath
	// that would fight KubeVirt's own restore controller, and re-rendering the
	// spec would undo the restore. The operation is found rather than
	// announced, so this status has exactly one writer.
	active, err := r.activeOperation(ctx, vm)
	if err != nil {
		return ctrl.Result{}, err
	}
	vm.Status.OperationInProgress = active
	if active != "" {
		vm.Status.ObservedGeneration = vm.Generation
		if err := kube.UpdateStatus(ctx, r.Client, vmControllerName, vm, before); err != nil {
			return ctrl.Result{}, fmt.Errorf("updating status while yielding: %w", err)
		}
		log.V(1).Info("Yielding to an operation", "operation", active)
		return ctrl.Result{}, nil
	}

	if !controllerutil.ContainsFinalizer(vm, vmFinalizer) {
		controllerutil.AddFinalizer(vm, vmFinalizer)
		if err := r.Update(ctx, vm); err != nil {
			return ctrl.Result{}, fmt.Errorf("adding finalizer: %w", err)
		}
		return ctrl.Result{Requeue: true}, nil
	}

	existing, err := r.existingVM(ctx, vm)
	if err != nil {
		return ctrl.Result{}, err
	}

	in, blocker, err := r.buildInput(ctx, vm, existing)
	if err != nil {
		return ctrl.Result{}, err
	}
	if blocker != nil {
		r.setBlocked(vm, blocker)
		if err := kube.UpdateStatus(ctx, r.Client, vmControllerName, vm, before); err != nil {
			return ctrl.Result{}, fmt.Errorf("updating blocked status: %w", err)
		}
		if blocker.Fatal {
			r.event(vm, corev1.EventTypeWarning, blocker.Reason, blocker.Message)
			return ctrl.Result{}, nil
		}
		return ctrl.Result{RequeueAfter: blockedVMRequeue}, nil
	}

	if existing == nil {
		created, err := r.createVM(ctx, vm, in)
		if err != nil {
			return ctrl.Result{}, err
		}
		existing = created
	} else {
		// A machine that shares the name but not the ownership stamp was put
		// there by someone else — an install that predates the migration, or a
		// person. Taking it over silently would mean this resource later
		// deletes a machine it never created. Adoption is a deliberate act.
		owner := existing.Labels[naming.OwnerUIDLabel]
		if owner != string(vm.UID) && vm.Annotations[naming.AdoptAnnotation] != existing.Name {
			r.setBlocked(vm, &blocked{
				Reason: "VirtualMachineConflict",
				Message: fmt.Sprintf(
					"VirtualMachine %s/%s already exists and was not created by this resource; "+
						"annotate with %s: %s to adopt it",
					vm.Namespace, existing.Name, naming.AdoptAnnotation, existing.Name),
				Fatal: true,
			})
			r.event(vm, corev1.EventTypeWarning, "VirtualMachineConflict",
				fmt.Sprintf("VirtualMachine %s exists and is not owned by this resource", existing.Name))
			if err := kube.UpdateStatus(ctx, r.Client, vmControllerName, vm, before); err != nil {
				return ctrl.Result{}, fmt.Errorf("updating conflict status: %w", err)
			}
			return ctrl.Result{}, nil
		}
		if err := r.reconcileExistingVM(ctx, vm, existing, in); err != nil {
			return ctrl.Result{}, err
		}
	}

	vm.Status.VirtualMachineName = existing.Name
	if len(existing.Spec.DataVolumeTemplates) > 0 {
		// Read the disk's name off the object rather than recomputing it: after
		// a restore the disk on the VM is not the one this controller rendered,
		// and status must describe what is there.
		vm.Status.RootDiskName = existing.Spec.DataVolumeTemplates[0].Name
	}
	apimeta.SetStatusCondition(&vm.Status.Conditions, metav1.Condition{
		Type:               platformv1alpha1.ConditionProvisioned,
		Status:             metav1.ConditionTrue,
		Reason:             "Rendered",
		Message:            fmt.Sprintf("VirtualMachine %s/%s exists", existing.Namespace, existing.Name),
		ObservedGeneration: vm.Generation,
	})
	apimeta.SetStatusCondition(&vm.Status.Conditions, metav1.Condition{
		Type:               platformv1alpha1.ConditionImageReady,
		Status:             metav1.ConditionTrue,
		Reason:             "Available",
		Message:            fmt.Sprintf("cloning from %s/%s", in.GoldenPVCNamespace, in.GoldenPVCName),
		ObservedGeneration: vm.Generation,
	})
	vm.Status.ObservedGeneration = vm.Generation

	if err := kube.UpdateStatus(ctx, r.Client, vmControllerName, vm, before); err != nil {
		return ctrl.Result{}, fmt.Errorf("updating status: %w", err)
	}
	return ctrl.Result{}, nil
}

// reconcileDelete removes the machine this resource describes.
//
// A person deleting a VM in the UI means the machine, not the paperwork. The
// cascade runs here rather than through an ownerReference so that it is the
// controller's decision and can be opted out of: strip the finalizer and the
// machine outlives the resource, which is what a migration rollback needs.
//
// Only objects carrying this resource's ownership stamp are removed. A machine
// that merely shares the name — adopted from before the migration, or recreated
// by someone else — is left alone.
func (r *ManagedVMReconciler) reconcileDelete(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(vm, vmFinalizer) {
		return ctrl.Result{}, nil
	}

	existing, err := r.existingVM(ctx, vm)
	if err != nil {
		return ctrl.Result{}, err
	}
	if existing != nil && existing.Labels[naming.OwnerUIDLabel] == string(vm.UID) {
		if err := kube.Delete(ctx, r.Client, vmControllerName, existing); err != nil {
			return ctrl.Result{}, fmt.Errorf("deleting VirtualMachine %s/%s: %w",
				vm.Namespace, existing.Name, err)
		}
		r.event(vm, corev1.EventTypeNormal, "VirtualMachineDeleted",
			fmt.Sprintf("Deleted VirtualMachine %s", existing.Name))
	}

	if err := r.deleteOwnedPasswordSecret(ctx, vm); err != nil {
		return ctrl.Result{}, err
	}

	controllerutil.RemoveFinalizer(vm, vmFinalizer)
	if err := r.Update(ctx, vm); err != nil {
		return ctrl.Result{}, fmt.Errorf("removing finalizer: %w", err)
	}
	return ctrl.Result{}, nil
}

// deleteOwnedPasswordSecret removes the first-boot password once the machine it
// was for is gone. The secret is created by whoever translated the request, and
// cleaned up here so there is one owner of the cleanup rather than two.
func (r *ManagedVMReconciler) deleteOwnedPasswordSecret(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) error {
	ref := vm.Spec.InitialPasswordSecretRef
	if ref == nil {
		return nil
	}
	secret := &corev1.Secret{}
	err := r.Get(ctx, types.NamespacedName{Namespace: vm.Namespace, Name: ref.Name}, secret)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("reading password secret: %w", err)
	}
	if secret.Labels[naming.OwnerNameLabel] != vm.Name {
		// Someone else's secret that this resource merely points at.
		return nil
	}
	return kube.Delete(ctx, r.Client, vmControllerName, secret)
}

// activeOperation names the unfinished operation acting on this machine, if
// there is one.
func (r *ManagedVMReconciler) activeOperation(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) (string, error) {
	ops := &platformv1alpha1.ManagedVMOperationList{}
	if err := r.List(ctx, ops,
		client.InNamespace(vm.Namespace),
		client.MatchingFields{operationVMIndex: vm.Name},
	); err != nil {
		return "", fmt.Errorf("looking for operations on %s/%s: %w", vm.Namespace, vm.Name, err)
	}
	for i := range ops.Items {
		op := &ops.Items[i]
		if !op.DeletionTimestamp.IsZero() || op.Status.Finished() {
			continue
		}
		return op.Name, nil
	}
	return "", nil
}

func (r *ManagedVMReconciler) existingVM(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) (*kubevirtv1.VirtualMachine, error) {
	found := &kubevirtv1.VirtualMachine{}
	err := r.Get(ctx, types.NamespacedName{Namespace: vm.Namespace, Name: vm.Name}, found)
	switch {
	case err == nil:
		return found, nil
	case apierrors.IsNotFound(err):
		return nil, nil
	default:
		return nil, fmt.Errorf("reading VirtualMachine %s/%s: %w", vm.Namespace, vm.Name, err)
	}
}

// buildInput gathers everything the renderer needs, or the reason it cannot.
func (r *ManagedVMReconciler) buildInput(
	ctx context.Context, vm *platformv1alpha1.ManagedVM, existing *kubevirtv1.VirtualMachine,
) (kubevirt.Input, *blocked, error) {
	in := kubevirt.Input{VM: vm, VNC: true}

	if b := r.resolveSource(ctx, vm, &in); b != nil {
		return in, b, nil
	}

	// The spec overrides whatever the template supplied.
	if c := vm.Spec.Compute; c != nil {
		in.Cores, in.Sockets, in.Threads, in.Memory = c.Cores, c.Sockets, c.Threads, c.Memory
	}
	if d := vm.Spec.RootDisk; d != nil {
		in.DiskSize = d.Size
		in.StorageClass = d.StorageClass
	}
	if in.Cores == 0 || in.Memory == "" || in.DiskSize == "" {
		return in, &blocked{
			Reason:  "IncompleteSpec",
			Message: "cores, memory and root disk size are all required, from the template or from the spec",
			Fatal:   true,
		}, nil
	}
	if c := vm.Spec.Console; c != nil {
		if c.VNC != nil {
			in.VNC = *c.VNC
		}
		if c.Serial != nil {
			in.Serial = *c.Serial
		}
	}

	if b := r.resolveNetworks(ctx, vm, &in); b != nil {
		return in, b, nil
	}

	password, b := r.resolvePassword(ctx, vm)
	if b != nil {
		return in, b, nil
	}
	var keys []string
	if vm.Spec.SSH != nil {
		keys = vm.Spec.SSH.AuthorizedKeys
	}
	var vmUserData string
	if vm.Spec.CloudInit != nil {
		vmUserData = vm.Spec.CloudInit.UserData
	}
	in.CloudInitUserData = kubevirt.MergeCloudInit(in.CloudInitUserData, vmUserData, keys, password)

	overcommit, err := r.cpuOvercommit(ctx)
	if err != nil {
		return in, nil, err
	}
	in.CPUOvercommit = overcommit

	ns := &corev1.Namespace{}
	if err := r.Get(ctx, types.NamespacedName{Name: vm.Namespace}, ns); err != nil {
		return in, nil, fmt.Errorf("reading namespace %s: %w", vm.Namespace, err)
	}
	in.Project = ns.Labels[naming.ProjectLabel]
	in.Environment = ns.Labels[naming.EnvironmentLabel]
	in.Owner = vm.Annotations["kubevirt-ui.io/owner"]

	in.RootDiskName = r.rootDiskName(vm, existing)
	return in, nil, nil
}

// rootDiskName keeps the name of a disk that already exists, and derives one
// otherwise.
//
// The epoch in a derived name is what stops a replacement disk from colliding
// with a predecessor that is still terminating; keeping the existing name is
// what stops this controller from arguing with a restore that renamed it.
func (r *ManagedVMReconciler) rootDiskName(
	vm *platformv1alpha1.ManagedVM, existing *kubevirtv1.VirtualMachine,
) string {
	if existing != nil && len(existing.Spec.DataVolumeTemplates) > 0 {
		return existing.Spec.DataVolumeTemplates[0].Name
	}
	if vm.Status.RootDiskName != "" {
		return vm.Status.RootDiskName
	}
	epoch := vm.Status.RootDiskEpoch
	if epoch == 0 {
		epoch = 1
	}
	return fmt.Sprintf("%s-root-%d", vm.Name, epoch)
}

func (r *ManagedVMReconciler) resolvePassword(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) (string, *blocked) {
	ref := vm.Spec.InitialPasswordSecretRef
	if ref == nil {
		return "", nil
	}
	secret := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: vm.Namespace, Name: ref.Name}, secret); err != nil {
		if apierrors.IsNotFound(err) {
			return "", &blocked{
				Reason:  "PasswordSecretNotFound",
				Message: fmt.Sprintf("Secret %s/%s does not exist", vm.Namespace, ref.Name),
			}
		}
		return "", &blocked{Reason: "PasswordSecretUnreadable", Message: err.Error()}
	}
	key := ref.Key
	if key == "" {
		key = "password"
	}
	value, ok := secret.Data[key]
	if !ok {
		return "", &blocked{
			Reason:  "PasswordSecretIncomplete",
			Message: fmt.Sprintf("Secret %s/%s has no key %q", vm.Namespace, ref.Name, key),
			Fatal:   true,
		}
	}
	return string(value), nil
}

func (r *ManagedVMReconciler) cpuOvercommit(ctx context.Context) (int, error) {
	cm := &corev1.ConfigMap{}
	err := r.Get(ctx, types.NamespacedName{Namespace: systemNamespace, Name: settingsConfigMap}, cm)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return 1, nil
		}
		return 0, fmt.Errorf("reading cluster settings: %w", err)
	}
	var settings struct {
		CPUOvercommit int `json:"cpu_overcommit"`
	}
	if raw, ok := cm.Data["settings"]; ok && raw != "" {
		if err := json.Unmarshal([]byte(raw), &settings); err != nil {
			// A malformed settings document must not silently mean "no
			// overcommit" — that would quietly change how densely the cluster
			// packs. Report it and use the safe value.
			return 1, nil
		}
	}
	if settings.CPUOvercommit < 1 {
		return 1, nil
	}
	return settings.CPUOvercommit, nil
}

func (r *ManagedVMReconciler) createVM(
	ctx context.Context, vm *platformv1alpha1.ManagedVM, in kubevirt.Input,
) (*kubevirtv1.VirtualMachine, error) {
	desired, err := kubevirt.VirtualMachine(in)
	if err != nil {
		return nil, fmt.Errorf("rendering VirtualMachine: %w", err)
	}
	if err := r.Create(ctx, desired); err != nil {
		if apierrors.IsAlreadyExists(err) {
			// Lost a race with ourselves; the next pass adopts it.
			return r.existingVM(ctx, vm)
		}
		return nil, fmt.Errorf("creating VirtualMachine %s/%s: %w", vm.Namespace, vm.Name, err)
	}
	kube.CountWrite(r.Scheme, desired, vmControllerName, "created")
	r.event(vm, corev1.EventTypeNormal, "VirtualMachineCreated",
		fmt.Sprintf("Created VirtualMachine %s with root disk %s", desired.Name, in.RootDiskName))
	return desired, nil
}

// reconcileExistingVM keeps the parts of an existing VirtualMachine that this
// controller owns, and nothing else.
//
// What it deliberately never touches: volumes, dataVolumeTemplates and the disk
// list. Hot-plugging writes those arrays, and an upstream restore rewrites them
// to the names of the restored disks. A controller that re-rendered them would
// undo a restore and detach a hot-plugged disk, on a timer, with no user action
// in sight.
func (r *ManagedVMReconciler) reconcileExistingVM(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	existing *kubevirtv1.VirtualMachine,
	in kubevirt.Input,
) error {
	wantLabels := kubevirt.Labels(in)
	wantAnnotations := kubevirt.Annotations(in)
	wantRunStrategy := kubevirtv1.RunStrategyHalted
	if vm.Spec.Running {
		wantRunStrategy = kubevirtv1.RunStrategyAlways
	}

	overridden := existing.Spec.RunStrategy != nil && *existing.Spec.RunStrategy != wantRunStrategy

	// Compute is reconciled, unlike the disk arrays. Nobody else writes cores
	// and memory, and leaving them alone would make editing them a silent
	// no-op — the precise defect this API exists to remove. Admission refuses
	// the edit while the machine is running, so by the time it lands here the
	// machine is stopped and will come up with the new size.
	desired, err := kubevirt.VirtualMachine(in)
	if err != nil {
		return fmt.Errorf("rendering VirtualMachine for comparison: %w", err)
	}

	patched := existing.DeepCopy()
	res, err := kube.Ensure(ctx, r.Client, vmControllerName, patched, func() error {
		if patched.Labels == nil {
			patched.Labels = map[string]string{}
		}
		for k, v := range wantLabels {
			patched.Labels[k] = v
		}
		if len(wantAnnotations) > 0 && patched.Annotations == nil {
			patched.Annotations = map[string]string{}
		}
		for k, v := range wantAnnotations {
			patched.Annotations[k] = v
		}
		patched.Spec.RunStrategy = &wantRunStrategy
		if patched.Spec.Template != nil && desired.Spec.Template != nil {
			patched.Spec.Template.Spec.Domain.CPU = desired.Spec.Template.Spec.Domain.CPU
			patched.Spec.Template.Spec.Domain.Memory = desired.Spec.Template.Spec.Domain.Memory
			patched.Spec.Template.Spec.Domain.Resources = desired.Spec.Template.Spec.Domain.Resources
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("reconciling VirtualMachine %s/%s: %w", existing.Namespace, existing.Name, err)
	}

	// Someone else set the power state — virtctl, a script, a person. It is
	// reverted, because the resource is the desired state, but never quietly:
	// an unexplained VM that will not stay stopped is worse than a refusal.
	if overridden && res != controllerutil.OperationResultNone {
		r.event(vm, corev1.EventTypeWarning, "RunStrategyOverridden",
			fmt.Sprintf("VirtualMachine runStrategy was changed outside this resource and has been reset to %s; "+
				"set spec.running instead", wantRunStrategy))
	}
	*existing = *patched
	return nil
}

func (r *ManagedVMReconciler) setBlocked(vm *platformv1alpha1.ManagedVM, b *blocked) {
	condType := platformv1alpha1.ConditionProvisioned
	if isImageReason(b.Reason) {
		condType = platformv1alpha1.ConditionImageReady
		apimeta.SetStatusCondition(&vm.Status.Conditions, metav1.Condition{
			Type:               platformv1alpha1.ConditionProvisioned,
			Status:             metav1.ConditionFalse,
			Reason:             "WaitingForImage",
			Message:            b.Message,
			ObservedGeneration: vm.Generation,
		})
	}
	apimeta.SetStatusCondition(&vm.Status.Conditions, metav1.Condition{
		Type:               condType,
		Status:             metav1.ConditionFalse,
		Reason:             b.Reason,
		Message:            b.Message,
		ObservedGeneration: vm.Generation,
	})
	vm.Status.ObservedGeneration = vm.Generation
}

func isImageReason(reason string) bool {
	switch reason {
	case "ImageNotFound", "ImageNotReady", "ImageUnreadable", "ImageAccessDenied",
		"TemplateNotFound", "TemplateStoreMissing", "TemplateStoreUnreadable",
		"TemplateUnreadable", "TemplateHasNoImage":
		return true
	}
	return false
}

func (r *ManagedVMReconciler) event(obj client.Object, eventType, reason, message string) {
	if r.Recorder == nil {
		return
	}
	r.Recorder.Event(obj, eventType, reason, message)
}

// operationVMIndex indexes operations by the machine they act on.
const operationVMIndex = "spec.vmName"

// SetupWithManager wires the controller.
func (r *ManagedVMReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if err := mgr.GetFieldIndexer().IndexField(
		context.Background(), &platformv1alpha1.ManagedVMOperation{}, operationVMIndex,
		func(obj client.Object) []string {
			op, ok := obj.(*platformv1alpha1.ManagedVMOperation)
			if !ok || op.Spec.VMName == "" {
				return nil
			}
			return []string{op.Spec.VMName}
		},
	); err != nil {
		return fmt.Errorf("indexing operations by machine: %w", err)
	}

	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.ManagedVM{}).
		// An operation starting or finishing changes whether this controller
		// may touch the machine, so the yield lifts as soon as it is over
		// rather than at the next unrelated event.
		Watches(&platformv1alpha1.ManagedVMOperation{},
			handler.EnqueueRequestsFromMapFunc(mapOperationToVM)).
		Watches(&kubevirtv1.VirtualMachine{}, handler.EnqueueRequestsFromMapFunc(mapOwnedToManagedVM)).
		// An image becoming Ready is what unblocks every VM waiting on it, so
		// the wait costs nothing and needs no polling.
		Watches(&platformv1alpha1.ManagedImage{}, handler.EnqueueRequestsFromMapFunc(r.mapImageToVMs)).
		Named(vmControllerName).
		Complete(r)
}

func mapOperationToVM(_ context.Context, obj client.Object) []reconcile.Request {
	op, ok := obj.(*platformv1alpha1.ManagedVMOperation)
	if !ok || op.Spec.VMName == "" {
		return nil
	}
	return []reconcile.Request{{
		NamespacedName: types.NamespacedName{Namespace: op.Namespace, Name: op.Spec.VMName},
	}}
}

func mapOwnedToManagedVM(_ context.Context, obj client.Object) []reconcile.Request {
	labels := obj.GetLabels()
	if labels[naming.OwnerKindLabel] != "ManagedVM" {
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

func (r *ManagedVMReconciler) mapImageToVMs(ctx context.Context, obj client.Object) []reconcile.Request {
	list := &platformv1alpha1.ManagedVMList{}
	if err := r.List(ctx, list); err != nil {
		return nil
	}
	var out []reconcile.Request
	for i := range list.Items {
		vm := &list.Items[i]
		ref := vm.Spec.ImageRef
		if ref == nil {
			continue
		}
		ns := ref.Namespace
		if ns == "" {
			ns = vm.Namespace
		}
		if ns == obj.GetNamespace() && ref.Name == obj.GetName() {
			out = append(out, reconcile.Request{
				NamespacedName: types.NamespacedName{Namespace: vm.Namespace, Name: vm.Name},
			})
		}
	}
	return out
}
