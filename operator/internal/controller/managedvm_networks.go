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

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

// sweepDetachedNICs removes network entries left behind by an unplug.
//
// Detaching a NIC marks the interface `state: absent`, which is how KubeVirt is
// asked to unplug it — but nothing ever removed the matching entry from
// `networks`. The litter accumulates, and because a network name has to be
// unique, attaching a NIC with the same name again is refused: the machine
// still lists a network for an interface that has been gone for months.
//
// This removes only what is provably dead, and never adds or reorders anything:
//
//   - a network with no interface at all — nothing can be using it;
//   - an interface marked absent whose unplug has finished, which is either a
//     stopped machine or a running one that no longer reports the interface.
//
// An interface marked absent on a machine that still reports it is mid-unplug
// and left alone; removing the entries then would take the request away before
// KubeVirt has acted on it.
func (r *ManagedVMReconciler) sweepDetachedNICs(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	existing *kubevirtv1.VirtualMachine,
) error {
	if existing.Spec.Template == nil {
		return nil
	}
	spec := &existing.Spec.Template.Spec

	live, err := r.liveInterfaceNames(ctx, vm, existing)
	if err != nil {
		return err
	}

	byName := make(map[string]kubevirtv1.Interface, len(spec.Domain.Devices.Interfaces))
	for _, iface := range spec.Domain.Devices.Interfaces {
		byName[iface.Name] = iface
	}

	dead := map[string]struct{}{}
	for _, network := range spec.Networks {
		iface, present := byName[network.Name]
		if !present {
			dead[network.Name] = struct{}{}
			continue
		}
		if iface.State != kubevirtv1.InterfaceStateAbsent {
			continue
		}
		if _, stillThere := live[iface.Name]; stillThere {
			// The unplug is in progress. Taking the entries away now would
			// withdraw the request before KubeVirt has carried it out.
			continue
		}
		dead[network.Name] = struct{}{}
	}
	if len(dead) == 0 {
		return nil
	}

	patched := existing.DeepCopy()
	networks := patched.Spec.Template.Spec.Networks[:0:0]
	for _, network := range patched.Spec.Template.Spec.Networks {
		if _, drop := dead[network.Name]; drop {
			continue
		}
		networks = append(networks, network)
	}
	interfaces := patched.Spec.Template.Spec.Domain.Devices.Interfaces[:0:0]
	for _, iface := range patched.Spec.Template.Spec.Domain.Devices.Interfaces {
		if _, drop := dead[iface.Name]; drop {
			continue
		}
		interfaces = append(interfaces, iface)
	}
	patched.Spec.Template.Spec.Networks = networks
	patched.Spec.Template.Spec.Domain.Devices.Interfaces = interfaces

	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("clearing detached interfaces from %s/%s: %w",
			existing.Namespace, existing.Name, err)
	}
	kube.CountWrite(r.Scheme, patched, vmControllerName, "updated")
	*existing = *patched
	return nil
}

// liveInterfaceNames is what the running guest still has plugged in. A stopped
// machine has none, which is the simple case.
func (r *ManagedVMReconciler) liveInterfaceNames(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	existing *kubevirtv1.VirtualMachine,
) (map[string]struct{}, error) {
	vmi := &kubevirtv1.VirtualMachineInstance{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: existing.Namespace, Name: existing.Name,
	}, vmi)
	if apierrors.IsNotFound(err) {
		return map[string]struct{}{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading the running instance of %s/%s: %w",
			vm.Namespace, vm.Name, err)
	}

	live := make(map[string]struct{}, len(vmi.Status.Interfaces))
	for _, iface := range vmi.Status.Interfaces {
		live[iface.Name] = struct{}{}
	}
	return live, nil
}
