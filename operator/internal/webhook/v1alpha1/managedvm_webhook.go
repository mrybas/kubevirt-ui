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

// Package v1alpha1 holds the admission webhooks.
//
// The division of labour is deliberate and worth stating once: anything that
// can be decided from the object alone lives in the CRD schema as an OpenAPI
// rule or a CEL expression, because that costs no availability and produces no
// phantom diffs in a plan. A webhook is only for what needs to look at other
// objects. And anything that may legitimately become true later — an image that
// does not exist yet, a subnet being created in the same apply — is not an
// admission decision at all; it is a condition the controller reports and waits
// out. Apply order is not part of this API.
package v1alpha1

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
	"github.com/mrybas/kubevirt-ui/operator/internal/scope"
)

var managedvmlog = logf.Log.WithName("managedvm-resource")

var (
	subnetGVK = schema.GroupVersionKind{Group: "kubeovn.io", Version: "v1", Kind: "Subnet"}
	vpcGVK    = schema.GroupVersionKind{Group: "kubeovn.io", Version: "v1", Kind: "Vpc"}
)

const systemVPC = "ovn-cluster"

// SetupManagedVMWebhookWithManager registers the webhook for ManagedVM.
func SetupManagedVMWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr, &platformv1alpha1.ManagedVM{}).
		WithValidator(&ManagedVMCustomValidator{Client: mgr.GetClient()}).
		Complete()
}

// +kubebuilder:webhook:path=/validate-platform-kubevirt-ui-io-v1alpha1-managedvm,mutating=false,failurePolicy=fail,sideEffects=None,groups=platform.kubevirt-ui.io,resources=managedvms,verbs=create;update,versions=v1alpha1,name=vmanagedvm-v1alpha1.kb.io,admissionReviewVersions=v1

// ManagedVMCustomValidator checks the things that need other objects in hand.
type ManagedVMCustomValidator struct {
	client.Client
}

// ValidateCreate refuses a VM that can never work as written.
func (v *ManagedVMCustomValidator) ValidateCreate(
	ctx context.Context, obj *platformv1alpha1.ManagedVM,
) (admission.Warnings, error) {
	managedvmlog.V(1).Info("validating create", "name", obj.GetName())
	if err := v.validateNetworks(ctx, obj); err != nil {
		return nil, err
	}
	return nil, v.validateDisksAreNotShared(ctx, obj)
}

// ValidateUpdate additionally refuses changes the running machine cannot take.
func (v *ManagedVMCustomValidator) ValidateUpdate(
	ctx context.Context, oldObj, newObj *platformv1alpha1.ManagedVM,
) (admission.Warnings, error) {
	managedvmlog.V(1).Info("validating update", "name", newObj.GetName())

	// The network rules are checked on update as well as create. The path this
	// replaces checked them only when the VM was created, and hot-plugging went
	// straight past — which is how a VM ends up with a second VPC interface
	// that nothing serves.
	if err := v.validateNetworks(ctx, newObj); err != nil {
		return nil, err
	}

	if err := v.validateDisksAreNotShared(ctx, newObj); err != nil {
		return nil, err
	}

	if computeChanged(oldObj, newObj) && oldObj.Spec.Running {
		return nil, fmt.Errorf(
			"cores and memory can only be changed while the machine is stopped; " +
				"set spec.running to false first")
	}
	return nil, nil
}

// ValidateDelete has nothing to say: deleting the description of a machine is
// always allowed, and deliberately does not delete the machine.
func (v *ManagedVMCustomValidator) ValidateDelete(
	_ context.Context, _ *platformv1alpha1.ManagedVM,
) (admission.Warnings, error) {
	return nil, nil
}

func computeChanged(oldObj, newObj *platformv1alpha1.ManagedVM) bool {
	a, b := oldObj.Spec.Compute, newObj.Spec.Compute
	if a == nil || b == nil {
		return a != b
	}
	return *a != *b
}

// validateDisksAreNotShared refuses to attach a disk another machine holds.
//
// Two machines writing one filesystem corrupts it, and neither of them finds
// out. The path this replaces checked the same thing, but only on its own
// endpoint: anything else that attached a disk — a manifest, a script, the
// hot-plug path on a second machine — went straight past it.
func (v *ManagedVMCustomValidator) validateDisksAreNotShared(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) error {
	if len(vm.Spec.Disks) == 0 {
		return nil
	}

	others := &platformv1alpha1.ManagedVMList{}
	if err := v.List(ctx, others, client.InNamespace(vm.Namespace)); err != nil {
		return fmt.Errorf("listing machines in %s: %w", vm.Namespace, err)
	}

	holders := map[string]string{}
	for i := range others.Items {
		other := &others.Items[i]
		if other.Name == vm.Name || !other.DeletionTimestamp.IsZero() {
			continue
		}
		for _, disk := range other.Spec.Disks {
			holders[disk.Claim] = other.Name
		}
	}

	for _, disk := range vm.Spec.Disks {
		if holder, taken := holders[disk.Claim]; taken {
			return fmt.Errorf(
				"disk %q is already attached to %q; a disk written by two machines is "+
					"corrupted by both", disk.Claim, holder)
		}
	}
	return nil
}

// validateNetworks refuses what will never become valid: a subnet belonging to
// another folder, an infrastructure subnet, kube-ovn's own, or a VPC overlay
// asked for as a secondary interface.
//
// A subnet that simply does not exist yet is NOT refused here — one apply may
// create the network and the VM together, and the controller reports the wait.
func (v *ManagedVMCustomValidator) validateNetworks(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) error {
	if len(vm.Spec.Networks) == 0 {
		return nil
	}

	ns := &corev1.Namespace{}
	if err := v.Get(ctx, types.NamespacedName{Name: vm.Namespace}, ns); err != nil {
		// Cannot judge scope without the namespace. Refusing every VM because
		// of a transient read error would be worse than letting the controller
		// make the same call a moment later, where it can retry.
		managedvmlog.Info("namespace unreadable, deferring network checks to the controller",
			"namespace", vm.Namespace, "error", err)
		return nil
	}
	target := scope.Target{
		Folder:      ns.Labels[naming.FolderLabel],
		Environment: ns.Labels[naming.EnvironmentLabel],
	}

	for idx, nic := range vm.Spec.Networks {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := v.Get(ctx, types.NamespacedName{Name: nic.Subnet}, subnet); err != nil {
			if apierrors.IsNotFound(err) {
				// Legal: the subnet may be created by the same apply.
				continue
			}
			return fmt.Errorf("reading subnet %q: %w", nic.Subnet, err)
		}

		vlan, _, _ := unstructured.NestedString(subnet.Object, "spec", "vlan")
		vpc, _, _ := unstructured.NestedString(subnet.Object, "spec", "vpc")

		if vlan == "" && vpc != "" && vpc != systemVPC && idx != 0 {
			return fmt.Errorf(
				"subnet %q is a VPC overlay and can only be the first NIC; "+
					"use a VLAN-backed subnet for additional interfaces", nic.Subnet)
		}

		net := scope.Network{
			Name:    nic.Subnet,
			VPC:     vpc,
			VLAN:    vlan,
			Purpose: subnet.GetLabels()[scope.PurposeLabel],
		}
		if vpc != "" && vpc != systemVPC {
			vpcObj := &unstructured.Unstructured{}
			vpcObj.SetGroupVersionKind(vpcGVK)
			if err := v.Get(ctx, types.NamespacedName{Name: vpc}, vpcObj); err == nil {
				net.Folder = vpcObj.GetLabels()[naming.FolderLabel]
				net.Environment = vpcObj.GetLabels()[naming.EnvironmentLabel]
			} else {
				net.Folder = subnet.GetLabels()[naming.FolderLabel]
				net.Environment = subnet.GetLabels()[naming.EnvironmentLabel]
			}
		}

		if res := scope.Check(net, target); !res.Allowed {
			return fmt.Errorf("%s", res.Message)
		}
	}
	return nil
}
