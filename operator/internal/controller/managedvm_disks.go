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
	"strings"

	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	kubevirtv1 "kubevirt.io/api/core/v1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

// attachedToLabel records which machine holds a persistent disk. The disks page
// reads it, and the attach path used it as a cache in front of a full scan.
const attachedToLabel = "kubevirt-ui.io/attached-to"

// attachedToUIDLabel records *which* machine of that name holds it.
//
// A name is what people read, and it is what the disks page shows, so the label
// above keeps carrying one. But a name is reusable: delete a machine and create
// another with the same name and the claim would transfer silently. The UID
// pins the claim to one object, which is what lets a release be refused unless
// it comes from the holder itself.
const attachedToUIDLabel = "platform.kubevirt-ui.io/attached-to-uid"

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
		refused  []string
	)
	for name, disk := range desired {
		if _, ok := present[name]; ok {
			continue
		}
		// Claim before attaching, never after. Admission refuses a disk another
		// machine already declares, but admission is a preflight: two requests
		// racing each read a world in which the other has not landed yet, and
		// both pass. The claim is a compare-and-set on the disk itself, so at
		// most one of them can win it, whichever order they arrive in.
		holder, err := r.claimDisk(ctx, vm.Namespace, disk.Claim, vm.Name, string(vm.UID))
		if err != nil {
			return err
		}
		if holder != "" {
			refused = append(refused, fmt.Sprintf("%s (held by %s)", disk.Claim, holder))
			continue
		}
		toAttach = append(toAttach, disk)
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

	sort.Strings(refused)
	setDiskCondition(vm, refused)

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

	return r.releaseDetached(ctx, vm, desired, previously)
}

// claimDisk takes ownership of a disk, or reports who holds it.
//
// The label is the claim, and the write that sets it is a compare-and-set: the
// object carries the resourceVersion it was read at, so a second writer racing
// for the same disk is rejected by the API server rather than by luck. A
// conflict is returned as an error so the pass retries against a fresh read and
// sees whoever won.
func (r *ManagedVMReconciler) claimDisk(
	ctx context.Context, namespace, claim, holder, holderUID string,
) (string, error) {
	dv := &cdiv1.DataVolume{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: claim}, dv); err != nil {
		if apierrors.IsNotFound(err) {
			// A plain claim rather than a DataVolume: there is nothing to write
			// the holder on, and nothing this controller can arbitrate.
			return "", nil
		}
		return "", fmt.Errorf("reading disk %s/%s: %w", namespace, claim, err)
	}

	current := dv.Labels[attachedToLabel]
	currentUID := dv.Labels[attachedToUIDLabel]
	switch {
	case current == "":
		// Unclaimed.
	case current == holder && (currentUID == "" || currentUID == holderUID):
		// Ours already; adopt an entry left by an older release that recorded
		// no UID.
		if currentUID == holderUID {
			return "", nil
		}
	default:
		return current, nil
	}

	patched := dv.DeepCopy()
	if patched.Labels == nil {
		patched.Labels = map[string]string{}
	}
	patched.Labels[attachedToLabel] = holder
	patched.Labels[attachedToUIDLabel] = holderUID
	// Update rather than a patch, deliberately: it carries the resourceVersion
	// the object was read at, so the API server rejects a claimant whose read
	// predates somebody else's claim. A labels-only patch would carry no
	// version and the last writer would simply win — checked by mutating this
	// line and watching the stale-read test fail.
	if err := r.Update(ctx, patched); err != nil {
		if apierrors.IsConflict(err) {
			return "", fmt.Errorf("lost the race for disk %s/%s: %w", namespace, claim, err)
		}
		return "", fmt.Errorf("claiming disk %s/%s: %w", namespace, claim, err)
	}
	kube.CountWrite(r.Scheme, patched, vmControllerName, "updated")
	return "", nil
}

func setDiskCondition(vm *platformv1alpha1.ManagedVM, refused []string) {
	cond := metav1.Condition{
		Type:               platformv1alpha1.ConditionDisksAttached,
		Status:             metav1.ConditionTrue,
		Reason:             "Attached",
		Message:            "every declared disk is attached",
		ObservedGeneration: vm.Generation,
	}
	if len(refused) > 0 {
		cond.Status = metav1.ConditionFalse
		cond.Reason = "DiskHeldByAnotherMachine"
		cond.Message = "not attached: " + strings.Join(refused, ", ")
	}
	apimeta.SetStatusCondition(&vm.Status.Conditions, cond)
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

// releaseDetached drops the holder label from disks that are no longer wanted.
//
// A stale holder is why a disk belonging to a deleted machine could never be
// attached to anything again: the attach path reads the label before it scans.
func (r *ManagedVMReconciler) releaseDetached(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	desired map[string]platformv1alpha1.DiskAttachment,
	previously map[string]struct{},
) error {
	for name := range previously {
		if _, stillWanted := desired[name]; stillWanted {
			continue
		}
		// The recorded entry is the volume name; for the label it is the claim
		// that matters, and the two are the same unless someone chose
		// otherwise. Release by whichever name is there.
		if err := r.releaseDisk(ctx, vm.Namespace, name, vm.Name, string(vm.UID)); err != nil {
			return err
		}
	}
	return nil
}

// releaseDisk drops the holder labels, but only on behalf of the machine that
// holds them.
//
// A release that does not check would let one machine free another's disk
// simply by having once listed it — and the disk would then be attached twice,
// which is the exact outcome the claim exists to prevent. The UID is what makes
// the check exact: a name can be reused, an object cannot.
func (r *ManagedVMReconciler) releaseDisk(
	ctx context.Context, namespace, claim, holder, holderUID string,
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

	current, currentUID := dv.Labels[attachedToLabel], dv.Labels[attachedToUIDLabel]
	if current == "" {
		return nil
	}
	if current != holder {
		return nil
	}
	// Same name, different object: the claim belongs to a machine that replaced
	// this one, and freeing it would hand its disk to somebody else.
	if currentUID != "" && holderUID != "" && currentUID != holderUID {
		return nil
	}

	patched := dv.DeepCopy()
	delete(patched.Labels, attachedToLabel)
	delete(patched.Labels, attachedToUIDLabel)
	if err := r.Update(ctx, patched); err != nil {
		return fmt.Errorf("releasing disk %s/%s: %w", namespace, claim, err)
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
		if err := r.releaseDisk(ctx, vm.Namespace, claim, vm.Name, string(vm.UID)); err != nil {
			return err
		}
	}
	return nil
}
