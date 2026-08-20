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
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	kubevirtv1 "kubevirt.io/api/core/v1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

func newManagedVM(ns, name, imageName string) *platformv1alpha1.ManagedVM {
	return &platformv1alpha1.ManagedVM{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: platformv1alpha1.ManagedVMSpec{
			DisplayName: "Web 01",
			ImageRef:    &platformv1alpha1.ImageRef{Name: imageName},
			Compute:     &platformv1alpha1.ComputeSpec{Cores: 2, Sockets: 1, Threads: 1, Memory: "4Gi"},
			RootDisk:    &platformv1alpha1.RootDiskSpec{Size: "20Gi"},
			Running:     true,
		},
	}
}

// readyImage creates a ManagedImage and drives its disk to Succeeded, which is
// what the controller waits for.
func readyImage(t *testing.T, ns, name string) *platformv1alpha1.ManagedImage {
	t.Helper()
	img := newImage(ns, name)
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, ns, name, cdiv1.Succeeded, nil, "100.0%")
	eventually(t, "image "+name+" to be Ready", func() error {
		got := getImage(t, ns, name)
		if got.Status.Phase != platformv1alpha1.ImagePhaseReady {
			return fmt.Errorf("phase = %q", got.Status.Phase)
		}
		return nil
	})
	return img
}

func getVM(t *testing.T, ns, name string) *platformv1alpha1.ManagedVM {
	t.Helper()
	vm := &platformv1alpha1.ManagedVM{}
	deadline := time.Now().Add(10 * time.Second)
	for {
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, vm)
		if err == nil {
			return vm
		}
		if !apierrors.IsNotFound(err) || time.Now().After(deadline) {
			t.Fatalf("reading ManagedVM %s/%s: %v", ns, name, err)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// touchVM edits the resource so the controller definitely reconciles again,
// retrying the write: the controller is updating status at the same time, and
// losing that race is normal optimistic concurrency, not a finding.
func touchVM(t *testing.T, ns, name, displayName string) {
	t.Helper()
	eventually(t, "the resource to accept an edit", func() error {
		vm := getVM(t, ns, name)
		vm.Spec.DisplayName = displayName
		return k8sClient.Update(testCtx, vm)
	})
}

func getKubeVirtVM(ns, name string) (*kubevirtv1.VirtualMachine, error) {
	vm := &kubevirtv1.VirtualMachine{}
	err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: name}, vm)
	return vm, err
}

func mustSubnet(t *testing.T, name, vpc, vlan, dhcpOptions string) {
	t.Helper()
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	subnet.SetName(name)
	spec := map[string]any{
		"cidrBlock": "10.200.0.0/22",
		"protocol":  "IPv4",
	}
	if vpc != "" {
		spec["vpc"] = vpc
	}
	if vlan != "" {
		spec["vlan"] = vlan
	}
	if dhcpOptions != "" {
		spec["dhcpV4Options"] = dhcpOptions
	}
	if err := unstructured.SetNestedMap(subnet.Object, spec, "spec"); err != nil {
		t.Fatalf("building subnet: %v", err)
	}
	if err := k8sClient.Create(testCtx, subnet); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating subnet %s: %v", name, err)
	}
}

// The VirtualMachine is a one-to-one child, so it carries the resource's name.
// Anything else means a caller has to read a name back before it can reference
// the machine it just asked for — the problem this API exists to remove.
func TestTheVirtualMachineTakesTheResourceName(t *testing.T) {
	ns := "vm-naming"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "web-01", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the VirtualMachine to be rendered", func() error {
		vm, err := getKubeVirtVM(ns, "web-01")
		if err != nil {
			return err
		}
		if vm.Labels[naming.OwnerKindLabel] != "ManagedVM" {
			return fmt.Errorf("ownership not stamped: %v", vm.Labels)
		}
		if vm.Labels["kubevirt-ui.io/vm-name"] != "web-01" {
			return fmt.Errorf("vm-name label = %q; backup selection targets it, so it cannot be best-effort",
				vm.Labels["kubevirt-ui.io/vm-name"])
		}
		return nil
	})

	eventually(t, "status to name the machine and its disk", func() error {
		got := getVM(t, ns, "web-01")
		if got.Status.VirtualMachineName != "web-01" {
			return fmt.Errorf("status.virtualMachineName = %q", got.Status.VirtualMachineName)
		}
		if got.Status.RootDiskName != "web-01-root-1" {
			return fmt.Errorf("status.rootDiskName = %q, want an epoch-suffixed name", got.Status.RootDiskName)
		}
		return nil
	})
}

// Apply order is not part of this API: one apply may create the VM and the
// image together, in either order, and the VM must wait rather than fail.
func TestAVMWaitsForAnImageThatDoesNotExistYet(t *testing.T) {
	ns := "vm-waits"
	mustNamespace(t, ns, "opdev")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "early-bird", "late-image")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the VM to say what it is waiting for", func() error {
		got := getVM(t, ns, "early-bird")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionImageReady)
		if cond == nil || cond.Reason != "ImageNotFound" {
			return fmt.Errorf("condition = %+v, want reason ImageNotFound", cond)
		}
		if !strings.Contains(cond.Message, "late-image") {
			return fmt.Errorf("message does not name the image: %q", cond.Message)
		}
		return nil
	})

	consistently(t, "no VirtualMachine to be created while waiting", 2*time.Second, func() error {
		if _, err := getKubeVirtVM(ns, "early-bird"); err == nil {
			return fmt.Errorf("a VM was rendered from an image that does not exist")
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	// The image arrives. Nothing else happens — no re-apply, no restart.
	readyImage(t, ns, "late-image")

	eventually(t, "the VM to provision itself once the image lands", func() error {
		if _, err := getKubeVirtVM(ns, "early-bird"); err != nil {
			return err
		}
		got := getVM(t, ns, "early-bird")
		if !apimeta.IsStatusConditionTrue(got.Status.Conditions, platformv1alpha1.ConditionProvisioned) {
			return fmt.Errorf("Provisioned is not true")
		}
		return nil
	})
}

// Cloning from a disk that is still importing produces a VM with half an
// operating system, and nothing later says why.
func TestAVMWaitsForAnUnfinishedImport(t *testing.T) {
	ns := "vm-waits-import"
	mustNamespace(t, ns, "opdev")

	img := newImage(ns, "slow-image")
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, ns, "slow-image", cdiv1.ImportInProgress, nil, "42.0%")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "impatient", "slow-image")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the VM to report the unfinished import", func() error {
		got := getVM(t, ns, "impatient")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionImageReady)
		if cond == nil || cond.Reason != "ImageNotReady" {
			return fmt.Errorf("condition = %+v, want reason ImageNotReady", cond)
		}
		return nil
	})

	setDVStatus(t, ns, "slow-image", cdiv1.Succeeded, nil, "100.0%")
	eventually(t, "the VM to proceed once the import finishes", func() error {
		_, err := getKubeVirtVM(ns, "impatient")
		return err
	})
}

// The disk arrays belong to nobody after create. Hot-plug writes them, and an
// upstream restore rewrites them to the names of the restored disks — a
// controller that re-rendered them would undo a restore and detach a plugged-in
// disk, on a timer, with no user action in sight.
func TestTheControllerNeverRewritesTheDiskArrays(t *testing.T) {
	ns := "vm-volumes"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "hotplugged", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the VirtualMachine to exist", func() error {
		_, err := getKubeVirtVM(ns, "hotplugged")
		return err
	})

	// Something else plugs a disk in, exactly as the hot-plug path does.
	eventually(t, "the extra disk to be attached", func() error {
		vm, err := getKubeVirtVM(ns, "hotplugged")
		if err != nil {
			return err
		}
		vm.Spec.Template.Spec.Volumes = append(vm.Spec.Template.Spec.Volumes, kubevirtv1.Volume{
			Name: "data-1",
			VolumeSource: kubevirtv1.VolumeSource{
				DataVolume: &kubevirtv1.DataVolumeSource{Name: "some-data-disk", Hotpluggable: true},
			},
		})
		vm.Spec.Template.Spec.Domain.Devices.Disks = append(vm.Spec.Template.Spec.Domain.Devices.Disks,
			kubevirtv1.Disk{
				Name:       "data-1",
				DiskDevice: kubevirtv1.DiskDevice{Disk: &kubevirtv1.DiskTarget{Bus: kubevirtv1.DiskBusVirtio}},
			})
		return k8sClient.Update(testCtx, vm)
	})

	// Poke the resource so the controller definitely runs again.
	touchVM(t, ns, "hotplugged", "Web 01 renamed")

	consistently(t, "the plugged-in disk to survive reconciliation", 5*time.Second, func() error {
		vm, err := getKubeVirtVM(ns, "hotplugged")
		if err != nil {
			return err
		}
		for _, v := range vm.Spec.Template.Spec.Volumes {
			if v.Name == "data-1" {
				return nil
			}
		}
		return fmt.Errorf("the controller detached a disk it did not attach")
	})
}

// A restore renames the root disk. The controller must describe what is there,
// not put back what it originally rendered.
func TestARenamedRootDiskIsAdoptedNotReverted(t *testing.T) {
	ns := "vm-restored"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "restored", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the VirtualMachine to exist", func() error {
		_, err := getKubeVirtVM(ns, "restored")
		return err
	})

	eventually(t, "the root disk to be renamed as a restore would", func() error {
		vm, err := getKubeVirtVM(ns, "restored")
		if err != nil {
			return err
		}
		vm.Spec.DataVolumeTemplates[0].Name = "restore-abc123"
		for i := range vm.Spec.Template.Spec.Volumes {
			if vm.Spec.Template.Spec.Volumes[i].DataVolume != nil {
				vm.Spec.Template.Spec.Volumes[i].DataVolume.Name = "restore-abc123"
			}
		}
		return k8sClient.Update(testCtx, vm)
	})

	touchVM(t, ns, "restored", "Restored")

	consistently(t, "the restored disk name to stand", 5*time.Second, func() error {
		vm, err := getKubeVirtVM(ns, "restored")
		if err != nil {
			return err
		}
		if vm.Spec.DataVolumeTemplates[0].Name != "restore-abc123" {
			return fmt.Errorf("the controller reverted the restore: disk is %q",
				vm.Spec.DataVolumeTemplates[0].Name)
		}
		return nil
	})

	eventually(t, "status to describe the disk that is actually there", func() error {
		got := getVM(t, ns, "restored")
		if got.Status.RootDiskName != "restore-abc123" {
			return fmt.Errorf("status.rootDiskName = %q", got.Status.RootDiskName)
		}
		return nil
	})
}

// The power state is declared on the resource, so an outside change is put
// back — but never silently. A machine that will not stay stopped and says
// nothing about why is worse than one that refuses.
func TestAnOutsidePowerChangeIsRevertedWithAnEvent(t *testing.T) {
	ns := "vm-runstrategy"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "stubborn", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the VirtualMachine to be running", func() error {
		vm, err := getKubeVirtVM(ns, "stubborn")
		if err != nil {
			return err
		}
		if vm.Spec.RunStrategy == nil || *vm.Spec.RunStrategy != kubevirtv1.RunStrategyAlways {
			return fmt.Errorf("runStrategy = %v", vm.Spec.RunStrategy)
		}
		return nil
	})

	// virtctl stop, or a script, or a person with kubectl.
	eventually(t, "the outside change to be applied", func() error {
		vm, err := getKubeVirtVM(ns, "stubborn")
		if err != nil {
			return err
		}
		halted := kubevirtv1.RunStrategyHalted
		vm.Spec.RunStrategy = &halted
		return k8sClient.Update(testCtx, vm)
	})

	eventually(t, "the declared state to be restored", func() error {
		vm, err := getKubeVirtVM(ns, "stubborn")
		if err != nil {
			return err
		}
		if vm.Spec.RunStrategy == nil || *vm.Spec.RunStrategy != kubevirtv1.RunStrategyAlways {
			return fmt.Errorf("runStrategy = %v", vm.Spec.RunStrategy)
		}
		return nil
	})

	eventually(t, "the revert to be announced, not silent", func() error {
		events := &corev1.EventList{}
		if err := k8sClient.List(testCtx, events, client.InNamespace(ns)); err != nil {
			return err
		}
		for _, e := range events.Items {
			if e.Reason == "RunStrategyOverridden" {
				return nil
			}
		}
		return fmt.Errorf("no RunStrategyOverridden event")
	})
}

// A private image is private. The check runs on every pass rather than once at
// admission, because the clone happens later and the scope can be narrowed
// after the VM was accepted.
func TestAPrivateImageCannotBeClonedFromAnotherNamespace(t *testing.T) {
	src, dst := "vm-scope-src", "vm-scope-dst"
	mustNamespace(t, src, "other-project")
	mustNamespace(t, dst, "opdev")
	readyImage(t, src, "private-image")

	vm := newManagedVM(dst, "thief", "private-image")
	vm.Spec.ImageRef.Namespace = src
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the refusal to name the reason", func() error {
		got := getVM(t, dst, "thief")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionImageReady)
		if cond == nil || cond.Reason != "ImageAccessDenied" {
			return fmt.Errorf("condition = %+v, want reason ImageAccessDenied", cond)
		}
		if !strings.Contains(cond.Message, "scope=environment") {
			return fmt.Errorf("message does not explain the refusal: %q", cond.Message)
		}
		return nil
	})

	consistently(t, "no VirtualMachine to be created", 2*time.Second, func() error {
		if _, err := getKubeVirtVM(dst, "thief"); err == nil {
			return fmt.Errorf("a VM was rendered against a refused image")
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})
}

func TestAProjectScopedImageIsShareableInsideItsProject(t *testing.T) {
	src, dst := "vm-shared-src", "vm-shared-dst"
	mustNamespace(t, src, "opdev")
	mustNamespace(t, dst, "opdev")

	img := newImage(src, "shared-image")
	img.Spec.Scope = "project"
	if err := k8sClient.Create(testCtx, img); err != nil {
		t.Fatalf("creating image: %v", err)
	}
	setDVStatus(t, src, "shared-image", cdiv1.Succeeded, nil, "100.0%")

	vm := newManagedVM(dst, "borrower", "shared-image")
	vm.Spec.ImageRef.Namespace = src
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the VM to clone across namespaces inside its project", func() error {
		got, err := getKubeVirtVM(dst, "borrower")
		if err != nil {
			return err
		}
		src := got.Spec.DataVolumeTemplates[0].Spec.Source.PVC
		if src == nil || src.Namespace != "vm-shared-src" || src.Name != "shared-image" {
			return fmt.Errorf("clone source = %+v", src)
		}
		return nil
	})
}

// A typo in a subnet name must be reported. The handler this replaces treated a
// failed lookup as "it must be a VLAN then", which wires the VM to an
// attachment that does not exist and shows up only as a guest with no address.
func TestAnUnknownSubnetIsReportedNotAssumed(t *testing.T) {
	ns := "vm-badnet"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	vm := newManagedVM(ns, "lost", "ubuntu")
	vm.Spec.Networks = []platformv1alpha1.NetworkAttachment{{Subnet: "typo-net"}}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the unknown subnet to be named", func() error {
		got := getVM(t, ns, "lost")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionProvisioned)
		if cond == nil || cond.Reason != "NetworkNotFound" {
			return fmt.Errorf("condition = %+v, want reason NetworkNotFound", cond)
		}
		if !strings.Contains(cond.Message, "typo-net") {
			return fmt.Errorf("message does not name the subnet: %q", cond.Message)
		}
		return nil
	})
}

// A VPC-attached guest is served by the launcher pod's resolver, and that pod
// gets the cluster resolver, which has no route from inside a VPC. The address
// is read from the datapath — the subnet's own DHCP options — not derived from
// the service CIDR by a formula that happens to be right on one cluster.
func TestAVPCGuestIsGivenTheResolverTheSubnetHandsOut(t *testing.T) {
	ns := "vm-vpcdns"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustSubnet(t, "tenant-overlay", "tenant-vpc", "",
		"lease_time=3600,router=10.200.0.1,server_id=10.200.0.1,dns_server=10.96.0.200")

	vm := newManagedVM(ns, "on-overlay", "ubuntu")
	vm.Spec.Networks = []platformv1alpha1.NetworkAttachment{{Subnet: "tenant-overlay"}}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the guest to be pointed at the VPC resolver", func() error {
		got, err := getKubeVirtVM(ns, "on-overlay")
		if err != nil {
			return err
		}
		spec := got.Spec.Template.Spec
		if spec.DNSPolicy != corev1.DNSNone {
			return fmt.Errorf("dnsPolicy = %q", spec.DNSPolicy)
		}
		if spec.DNSConfig == nil || len(spec.DNSConfig.Nameservers) != 1 ||
			spec.DNSConfig.Nameservers[0] != "10.96.0.200" {
			return fmt.Errorf("nameservers = %+v", spec.DNSConfig)
		}
		anns := got.Spec.Template.ObjectMeta.Annotations
		if anns["ovn.kubernetes.io/logical_switch"] != "tenant-overlay" {
			return fmt.Errorf("logical_switch annotation = %q", anns["ovn.kubernetes.io/logical_switch"])
		}
		if anns["kubevirt.io/allow-pod-bridge-network-live-migration"] != "true" {
			return fmt.Errorf("bridge-bound VM is missing the live-migration annotation")
		}
		return nil
	})
}

// Overcommit divides the request and leaves the limit alone: the guest keeps
// its full allowance while the scheduler packs by the divided figure.
func TestOvercommitDividesTheRequestNotTheLimit(t *testing.T) {
	ns := "vm-overcommit"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	settings := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: settingsConfigMap, Namespace: systemNamespace},
		Data:       map[string]string{"settings": `{"cpu_overcommit": 4}`},
	}
	if err := k8sClient.Create(testCtx, settings); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating settings: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, settings) })

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "packed", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the request to be divided by the ratio", func() error {
		got, err := getKubeVirtVM(ns, "packed")
		if err != nil {
			return err
		}
		res := got.Spec.Template.Spec.Domain.Resources
		if req := res.Requests.Cpu().String(); req != "500m" {
			return fmt.Errorf("cpu request = %q, want 500m (2 cores over a ratio of 4)", req)
		}
		if lim := res.Limits.Cpu().String(); lim != "2" {
			return fmt.Errorf("cpu limit = %q, want the full 2 cores", lim)
		}
		return nil
	})
}

// The cloud-init disk is attached even with nothing to configure, because it
// carries the network data that keeps a restored guest from losing its NIC.
func TestEveryGuestGetsTheNetworkDataDisk(t *testing.T) {
	ns := "vm-cloudinit"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "bare", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the cloud-init volume to carry netplan matched by name", func() error {
		got, err := getKubeVirtVM(ns, "bare")
		if err != nil {
			return err
		}
		for _, v := range got.Spec.Template.Spec.Volumes {
			if v.CloudInitNoCloud == nil {
				continue
			}
			if !strings.Contains(v.CloudInitNoCloud.NetworkData, `match:`) ||
				!strings.Contains(v.CloudInitNoCloud.NetworkData, `name: "e*"`) {
				return fmt.Errorf("network data does not match by name: %q", v.CloudInitNoCloud.NetworkData)
			}
			if v.CloudInitNoCloud.UserData != "" {
				return fmt.Errorf("a VM with nothing to configure got a user-data document")
			}
			return nil
		}
		return fmt.Errorf("no cloud-init volume attached")
	})
}
