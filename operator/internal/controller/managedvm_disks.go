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
	"sort"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

// attachedToLabel records which machine holds a persistent disk. The disks page
// reads it, and the attach path used it as a cache in front of a full scan.
const attachedToLabel = "kubevirt-ui.io/attached-to"

// reservedVolumes are the machine's own, rendered at create and never managed
// as attachments.
//
// The root entry is matched by volume *name*, never by the disk behind it: a
// restore replaces the disk and keeps the name — measured on a restored machine,
// where the volume was still called rootdisk and pointed at
// restore-447eb1df-…-rootdisk. Matching on the disk name would make every
// restore look like an unknown attachment.
var reservedVolumes = map[string]struct{}{
	"rootdisk":  {},
	"cloudinit": {},
}

// syncDisks brings the machine's attachments in line with the spec.
//
// It adds what the spec asks for and removes what this controller attached and
// the spec no longer asks for. Anything plugged in by another route stays: a
// reconciler that reclaims objects it did not create ends up deleting them.
func (r *ManagedVMReconciler) syncDisks(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	existing *kubevirtv1.VirtualMachine,
) error {
	// Keyed by the name the volume carries inside the machine, because that is
	// what the machine's own arrays are matched on.
	desired := make(map[string]platformv1alpha1.DiskAttachment, len(vm.Spec.Disks))
	for _, d := range vm.Spec.Disks {
		desired[volumeNameOf(d)] = d
	}
	previously := make(map[string]struct{}, len(vm.Status.AttachedDisks))
	for _, claim := range vm.Status.AttachedDisks {
		previously[claim] = struct{}{}
	}

	present := map[string]struct{}{}
	for _, vol := range existing.Spec.Template.Spec.Volumes {
		present[vol.Name] = struct{}{}
	}

	var (
		toAttach []platformv1alpha1.DiskAttachment
		toDetach []string
	)
	for name, disk := range desired {
		if _, ok := present[name]; !ok {
			toAttach = append(toAttach, disk)
		}
	}
	for name := range previously {
		if _, stillWanted := desired[name]; stillWanted {
			continue
		}
		if _, ok := present[name]; ok {
			toDetach = append(toDetach, name)
		}
	}

	if len(toAttach) > 0 || len(toDetach) > 0 {
		if err := r.applyDiskChanges(ctx, vm, existing, toAttach, toDetach); err != nil {
			return err
		}
	}

	// Record what is attached now, from the object rather than from intent: if
	// a write failed, the next pass should see that it failed.
	attached := make([]string, 0, len(desired))
	for _, vol := range existing.Spec.Template.Spec.Volumes {
		if _, reserved := reservedVolumes[vol.Name]; reserved {
			continue
		}
		if _, wanted := desired[vol.Name]; wanted {
			attached = append(attached, vol.Name)
		}
	}
	sort.Strings(attached)
	vm.Status.AttachedDisks = attached

	return r.syncAttachedLabels(ctx, vm, desired, previously)
}

func (r *ManagedVMReconciler) applyDiskChanges(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	existing *kubevirtv1.VirtualMachine,
	toAttach []platformv1alpha1.DiskAttachment,
	toDetach []string,
) error {
	patched := existing.DeepCopy()
	detach := make(map[string]struct{}, len(toDetach))
	for _, claim := range toDetach {
		detach[claim] = struct{}{}
	}

	volumes := patched.Spec.Template.Spec.Volumes[:0:0]
	for _, vol := range patched.Spec.Template.Spec.Volumes {
		if _, drop := detach[vol.Name]; drop {
			continue
		}
		volumes = append(volumes, vol)
	}
	disks := patched.Spec.Template.Spec.Domain.Devices.Disks[:0:0]
	for _, disk := range patched.Spec.Template.Spec.Domain.Devices.Disks {
		if _, drop := detach[disk.Name]; drop {
			continue
		}
		disks = append(disks, disk)
	}

	for _, want := range toAttach {
		bus := kubevirtv1.DiskBus(want.Bus)
		if bus == "" {
			bus = kubevirtv1.DiskBusVirtio
		}
		volumes = append(volumes, kubevirtv1.Volume{
			Name: volumeNameOf(want),
			VolumeSource: kubevirtv1.VolumeSource{
				DataVolume: &kubevirtv1.DataVolumeSource{
					Name: want.Claim,
					// Hotpluggable so that attaching and detaching does not
					// need the guest stopped, which is what the imperative path
					// did for a running machine and what people expect of a
					// disk that exists on its own.
					Hotpluggable: true,
				},
			},
		})
		disks = append(disks, kubevirtv1.Disk{
			Name:       volumeNameOf(want),
			DiskDevice: kubevirtv1.DiskDevice{Disk: &kubevirtv1.DiskTarget{Bus: bus}},
		})
	}

	patched.Spec.Template.Spec.Volumes = volumes
	patched.Spec.Template.Spec.Domain.Devices.Disks = disks

	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("changing the attached disks of %s/%s: %w",
			existing.Namespace, existing.Name, err)
	}
	kube.CountWrite(r.Scheme, patched, vmControllerName, "updated")
	*existing = *patched
	return nil
}

// syncAttachedLabels keeps the disks page honest about who holds what.
//
// The label is a cache in front of a full scan, and a stale one is why a disk
// belonging to a deleted machine could never be attached to anything again: the
// attach path checked the label before it scanned.
func (r *ManagedVMReconciler) syncAttachedLabels(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	desired map[string]platformv1alpha1.DiskAttachment,
	previously map[string]struct{},
) error {
	for _, disk := range desired {
		if err := r.setAttachedTo(ctx, vm.Namespace, disk.Claim, vm.Name); err != nil {
			return err
		}
	}
	for name := range previously {
		if _, stillWanted := desired[name]; stillWanted {
			continue
		}
		// The recorded entry is the volume name; for the label it is the claim
		// that matters, and the two are the same unless someone chose
		// otherwise. Release by whichever name is there.
		if err := r.setAttachedTo(ctx, vm.Namespace, name, ""); err != nil {
			return err
		}
	}
	return nil
}

func (r *ManagedVMReconciler) setAttachedTo(
	ctx context.Context, namespace, claim, holder string,
) error {
	dv := &cdiv1.DataVolume{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: claim}, dv); err != nil {
		if apierrors.IsNotFound(err) {
			// A plain claim rather than a DataVolume, or already gone. Neither
			// is this controller's to fix.
			return nil
		}
		return fmt.Errorf("reading disk %s/%s: %w", namespace, claim, err)
	}

	current := dv.Labels[attachedToLabel]
	if current == holder {
		return nil
	}
	// Do not take a label that names another machine: whoever holds it is
	// either right, or a stale entry this controller is not the one to judge.
	if holder != "" && current != "" && current != holder {
		return nil
	}

	patched := dv.DeepCopy()
	if patched.Labels == nil {
		patched.Labels = map[string]string{}
	}
	if holder == "" {
		delete(patched.Labels, attachedToLabel)
	} else {
		patched.Labels[attachedToLabel] = holder
	}
	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("recording the holder of %s/%s: %w", namespace, claim, err)
	}
	kube.CountWrite(r.Scheme, patched, vmControllerName, "updated")
	return nil
}

// volumeNameOf is the name a disk carries inside the machine.
func volumeNameOf(d platformv1alpha1.DiskAttachment) string {
	if d.Name != "" {
		return d.Name
	}
	return d.Claim
}

// releaseDisks drops the attachment labels when a machine goes away, so its
// disks can be attached to something else.
//
// Without this a disk outlives its machine as permanently unattachable: the
// attach path reads the label before it scans, and the label still names a VM
// that no longer exists.
func (r *ManagedVMReconciler) releaseDisks(
	ctx context.Context, vm *platformv1alpha1.ManagedVM,
) error {
	seen := map[string]struct{}{}
	for _, disk := range vm.Spec.Disks {
		seen[disk.Claim] = struct{}{}
	}
	for _, name := range vm.Status.AttachedDisks {
		seen[name] = struct{}{}
	}
	for claim := range seen {
		if err := r.setAttachedTo(ctx, vm.Namespace, claim, ""); err != nil {
			return err
		}
	}
	return nil
}
