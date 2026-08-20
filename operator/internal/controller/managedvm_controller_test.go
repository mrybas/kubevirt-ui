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
	"errors"
	"fmt"
	"strings"
	"sync"
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

func mustVPC(t *testing.T, name, folder, environment string) {
	t.Helper()
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	vpc.SetName(name)
	labels := map[string]string{}
	if folder != "" {
		labels[naming.FolderLabel] = folder
	}
	if environment != "" {
		labels[naming.EnvironmentLabel] = environment
	}
	vpc.SetLabels(labels)
	if err := unstructured.SetNestedMap(vpc.Object, map[string]any{}, "spec"); err != nil {
		t.Fatalf("building vpc: %v", err)
	}
	if err := k8sClient.Create(testCtx, vpc); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating vpc %s: %v", name, err)
	}
}

// Measured on the stand before this check existed: a VM in folder `opdev`
// attached to a VPC scoped to folder `poc-transit`. The wizard had always
// hidden that network; the create path took it. One rule, one implementation —
// this test and the wizard's now describe the same behaviour.
func TestAVMCannotAttachToAnotherFoldersNetwork(t *testing.T) {
	ns := "vm-scope-net"
	mustNamespace(t, ns, "opdev")
	// mustNamespace labels the environment dev; give it a folder too.
	eventually(t, "the namespace to carry a folder label", func() error {
		nsObj := &corev1.Namespace{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: ns}, nsObj); err != nil {
			return err
		}
		nsObj.Labels[naming.FolderLabel] = "opdev"
		return k8sClient.Update(testCtx, nsObj)
	})
	readyImage(t, ns, "ubuntu")
	mustVPC(t, "someone-elses-vpc", "poc-transit", "dev")
	mustSubnet(t, "someone-elses-subnet", "someone-elses-vpc", "", "")

	vm := newManagedVM(ns, "trespasser", "ubuntu")
	vm.Spec.Networks = []platformv1alpha1.NetworkAttachment{{Subnet: "someone-elses-subnet"}}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the refusal to name both folders", func() error {
		got := getVM(t, ns, "trespasser")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionProvisioned)
		if cond == nil || cond.Reason != "NetworkOutOfScope" {
			return fmt.Errorf("condition = %+v, want reason NetworkOutOfScope", cond)
		}
		if !strings.Contains(cond.Message, "poc-transit") {
			return fmt.Errorf("message does not name the owning folder: %q", cond.Message)
		}
		return nil
	})

	consistently(t, "no VirtualMachine to be created", 2*time.Second, func() error {
		if _, err := getKubeVirtVM(ns, "trespasser"); err == nil {
			return fmt.Errorf("a VM was attached to another folder's network")
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})
}

// A secondary VPC NIC would need a per-subnet attachment definition wrapping
// the OVN CNI, which does not exist. The create path refused this; the hot-plug
// path did not, which is how a VM ends up with an interface nothing serves.
func TestOnlyThePrimaryNICMayBeAVPCOverlay(t *testing.T) {
	ns := "vm-second-vpc"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustSubnet(t, "vlan-first", "", "vlan-300", "")
	mustSubnet(t, "overlay-second", "free-vpc", "", "")

	vm := newManagedVM(ns, "two-nics", "ubuntu")
	vm.Spec.Networks = []platformv1alpha1.NetworkAttachment{
		{Subnet: "vlan-first"},
		{Subnet: "overlay-second"},
	}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the second VPC NIC to be refused", func() error {
		got := getVM(t, ns, "two-nics")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionProvisioned)
		if cond == nil || cond.Reason != "VPCMustBePrimary" {
			return fmt.Errorf("condition = %+v, want reason VPCMustBePrimary", cond)
		}
		return nil
	})
}

// Deleting a VM in the UI means the machine, not the paperwork. The cascade is
// a finalizer rather than an ownerReference so it stays the controller's
// decision — strip the finalizer and the machine outlives the resource, which
// is what a migration rollback needs.
func TestDeletingTheResourceDeletesTheMachine(t *testing.T) {
	ns := "vm-cascade"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "doomed", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the VirtualMachine to exist", func() error {
		_, err := getKubeVirtVM(ns, "doomed")
		return err
	})

	if err := k8sClient.Delete(testCtx, getVM(t, ns, "doomed")); err != nil {
		t.Fatalf("deleting: %v", err)
	}

	eventually(t, "the machine to go with it", func() error {
		if _, err := getKubeVirtVM(ns, "doomed"); err == nil {
			return fmt.Errorf("the VirtualMachine outlived its resource")
		} else if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	eventually(t, "the resource itself to be released", func() error {
		vm := &platformv1alpha1.ManagedVM{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "doomed"}, vm)
		if err == nil {
			return fmt.Errorf("still held by finalizers %v", vm.Finalizers)
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})
}

// A machine that merely shares the name — adopted from before the migration, or
// put there by someone else — is not this resource's to delete.
func TestAMachineTheResourceDoesNotOwnIsLeftAlone(t *testing.T) {
	ns := "vm-not-mine"
	mustNamespace(t, ns, "opdev")

	stranger := &kubevirtv1.VirtualMachine{
		ObjectMeta: metav1.ObjectMeta{Name: "stranger", Namespace: ns},
		Spec: kubevirtv1.VirtualMachineSpec{
			RunStrategy: ptrTo(kubevirtv1.RunStrategyHalted),
			Template: &kubevirtv1.VirtualMachineInstanceTemplateSpec{
				Spec: kubevirtv1.VirtualMachineInstanceSpec{},
			},
		},
	}
	if err := k8sClient.Create(testCtx, stranger); err != nil {
		t.Fatalf("creating the stranger: %v", err)
	}

	readyImage(t, ns, "ubuntu")
	vm := newManagedVM(ns, "stranger", "ubuntu")
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating resource: %v", err)
	}
	eventually(t, "the collision to be reported rather than taken over", func() error {
		got := getVM(t, ns, "stranger")
		if len(got.Finalizers) == 0 {
			return fmt.Errorf("no finalizer yet")
		}
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionProvisioned)
		if cond == nil || cond.Reason != "VirtualMachineConflict" {
			return fmt.Errorf("condition = %+v, want reason VirtualMachineConflict", cond)
		}
		if !strings.Contains(cond.Message, naming.AdoptAnnotation) {
			return fmt.Errorf("the refusal does not say how to adopt: %q", cond.Message)
		}
		return nil
	})

	if err := k8sClient.Delete(testCtx, getVM(t, ns, "stranger")); err != nil {
		t.Fatalf("deleting: %v", err)
	}

	eventually(t, "the resource to be released", func() error {
		got := &platformv1alpha1.ManagedVM{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "stranger"}, got)
		if err == nil {
			return fmt.Errorf("still held")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	if _, err := getKubeVirtVM(ns, "stranger"); err != nil {
		t.Fatalf("a machine this resource never created was deleted with it: %v", err)
	}
}

func ptrTo[T any](v T) *T { return &v }

func mustDataVolume(t *testing.T, ns, name string) {
	t.Helper()
	dv := &cdiv1.DataVolume{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: ns,
			Labels:    map[string]string{"kubevirt-ui.io/managed": "true", "kubevirt-ui.io/persistent": "true"},
		},
		Spec: cdiv1.DataVolumeSpec{
			Source:  &cdiv1.DataVolumeSource{Blank: &cdiv1.DataVolumeBlankImage{}},
			Storage: &cdiv1.StorageSpec{},
		},
	}
	if err := k8sClient.Create(testCtx, dv); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating disk %s: %v", name, err)
	}
}

func volumeNames(vm *kubevirtv1.VirtualMachine) []string {
	var out []string
	for _, v := range vm.Spec.Template.Spec.Volumes {
		out = append(out, v.Name)
	}
	return out
}

// Attaching is declarative: what is plugged in is visible in one place and
// outlives whatever attached it.
func TestDisksAreAttachedAndDetachedFromTheSpec(t *testing.T) {
	ns := "vm-disks"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustDataVolume(t, ns, "data-one")

	vm := newManagedVM(ns, "with-disks", "ubuntu")
	vm.Spec.Disks = []platformv1alpha1.DiskAttachment{{Claim: "data-one", Bus: "virtio"}}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}

	eventually(t, "the disk to be attached and recorded", func() error {
		kvm, err := getKubeVirtVM(ns, "with-disks")
		if err != nil {
			return err
		}
		found := false
		for _, v := range kvm.Spec.Template.Spec.Volumes {
			if v.Name == "data-one" {
				found = true
				if v.DataVolume == nil || !v.DataVolume.Hotpluggable {
					return fmt.Errorf("attached, but not as a hot-pluggable disk")
				}
			}
		}
		if !found {
			return fmt.Errorf("volumes = %v", volumeNames(kvm))
		}
		got := getVM(t, ns, "with-disks")
		if len(got.Status.AttachedDisks) != 1 || got.Status.AttachedDisks[0] != "data-one" {
			return fmt.Errorf("status.attachedDisks = %v", got.Status.AttachedDisks)
		}
		// The disks page reads this label to say who holds what.
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "data-one"}, dv); err != nil {
			return err
		}
		if dv.Labels[attachedToLabel] != "with-disks" {
			return fmt.Errorf("attached-to = %q", dv.Labels[attachedToLabel])
		}
		return nil
	})

	// The machine's own disks are untouched by any of this.
	kvm, err := getKubeVirtVM(ns, "with-disks")
	if err != nil {
		t.Fatalf("reading machine: %v", err)
	}
	names := volumeNames(kvm)
	for _, want := range []string{"rootdisk", "cloudinit"} {
		found := false
		for _, n := range names {
			if n == want {
				found = true
			}
		}
		if !found {
			t.Fatalf("attaching a disk removed %s; volumes = %v", want, names)
		}
	}

	// Removing it from the spec detaches it and releases the label.
	touched := getVM(t, ns, "with-disks")
	touched.Spec.Disks = nil
	if err := k8sClient.Update(testCtx, touched); err != nil {
		t.Fatalf("detaching: %v", err)
	}

	eventually(t, "the disk to be detached and released", func() error {
		kvm, err := getKubeVirtVM(ns, "with-disks")
		if err != nil {
			return err
		}
		for _, v := range kvm.Spec.Template.Spec.Volumes {
			if v.Name == "data-one" {
				return fmt.Errorf("still attached")
			}
		}
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "data-one"}, dv); err != nil {
			return err
		}
		if _, held := dv.Labels[attachedToLabel]; held {
			return fmt.Errorf("the disk is still marked as held by %q", dv.Labels[attachedToLabel])
		}
		return nil
	})
}

// A disk plugged in by some other route is not this controller's to reclaim,
// and therefore not its to remove either.
func TestADiskAttachedElsewhereIsLeftAlone(t *testing.T) {
	ns := "vm-foreign-disk"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")

	if err := k8sClient.Create(testCtx, newManagedVM(ns, "tolerant", "ubuntu")); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the machine to exist", func() error {
		_, err := getKubeVirtVM(ns, "tolerant")
		return err
	})

	eventually(t, "a disk to be plugged in from outside", func() error {
		kvm, err := getKubeVirtVM(ns, "tolerant")
		if err != nil {
			return err
		}
		for _, v := range kvm.Spec.Template.Spec.Volumes {
			if v.Name == "outside-disk" {
				return nil
			}
		}
		kvm.Spec.Template.Spec.Volumes = append(kvm.Spec.Template.Spec.Volumes, kubevirtv1.Volume{
			Name:         "outside-disk",
			VolumeSource: kubevirtv1.VolumeSource{DataVolume: &kubevirtv1.DataVolumeSource{Name: "outside-disk"}},
		})
		return k8sClient.Update(testCtx, kvm)
	})

	touchVM(t, ns, "tolerant", "Tolerant renamed")

	consistently(t, "the foreign disk to survive", 5*time.Second, func() error {
		kvm, err := getKubeVirtVM(ns, "tolerant")
		if err != nil {
			return err
		}
		for _, v := range kvm.Spec.Template.Spec.Volumes {
			if v.Name == "outside-disk" {
				return nil
			}
		}
		return fmt.Errorf("the controller removed a disk it did not attach")
	})
}

// A machine that goes away must release its disks, or they stay unattachable:
// the attach path reads the holder label before it scans, and the label names a
// machine that no longer exists.
func TestDeletingAMachineReleasesItsDisks(t *testing.T) {
	ns := "vm-disk-release"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustDataVolume(t, ns, "shared-later")

	vm := newManagedVM(ns, "temporary", "ubuntu")
	vm.Spec.Disks = []platformv1alpha1.DiskAttachment{{Claim: "shared-later"}}
	if err := k8sClient.Create(testCtx, vm); err != nil {
		t.Fatalf("creating vm: %v", err)
	}
	eventually(t, "the disk to be held", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "shared-later"}, dv); err != nil {
			return err
		}
		if dv.Labels[attachedToLabel] != "temporary" {
			return fmt.Errorf("attached-to = %q", dv.Labels[attachedToLabel])
		}
		return nil
	})

	if err := k8sClient.Delete(testCtx, getVM(t, ns, "temporary")); err != nil {
		t.Fatalf("deleting: %v", err)
	}

	eventually(t, "the disk to be released", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "shared-later"}, dv); err != nil {
			return err
		}
		if _, held := dv.Labels[attachedToLabel]; held {
			return fmt.Errorf("still held by %q", dv.Labels[attachedToLabel])
		}
		return nil
	})
}

// Admission refuses a disk another machine already declares, but admission is a
// preflight: two requests racing each read a world in which the other has not
// landed, and both pass. The claim on the disk itself is what decides. Here the
// webhook is not running at all, which is precisely the situation to test.
func TestTwoMachinesRacingForOneDiskLeaveExactlyOneHolder(t *testing.T) {
	ns := "vm-disk-race"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustDataVolume(t, ns, "contested")

	for _, name := range []string{"racer-a", "racer-b"} {
		vm := newManagedVM(ns, name, "ubuntu")
		vm.Spec.Disks = []platformv1alpha1.DiskAttachment{{Claim: "contested"}}
		if err := k8sClient.Create(testCtx, vm); err != nil {
			t.Fatalf("creating %s: %v", name, err)
		}
	}

	eventually(t, "both machines to be rendered", func() error {
		for _, name := range []string{"racer-a", "racer-b"} {
			if _, err := getKubeVirtVM(ns, name); err != nil {
				return err
			}
		}
		return nil
	})

	eventually(t, "exactly one of them to hold the disk", func() error {
		holders := 0
		var loser string
		for _, name := range []string{"racer-a", "racer-b"} {
			kvm, err := getKubeVirtVM(ns, name)
			if err != nil {
				return err
			}
			attached := false
			for _, v := range kvm.Spec.Template.Spec.Volumes {
				if v.Name == "contested" {
					attached = true
				}
			}
			if attached {
				holders++
			} else {
				loser = name
			}
		}
		if holders != 1 {
			return fmt.Errorf("%d machines have the disk attached; exactly one may", holders)
		}

		// And the one that lost says so, naming who has it.
		got := getVM(t, ns, loser)
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionDisksAttached)
		if cond == nil || cond.Status != metav1.ConditionFalse {
			return fmt.Errorf("the machine without the disk does not say so: %+v", cond)
		}
		if !strings.Contains(cond.Message, "contested") || !strings.Contains(cond.Message, "held by") {
			return fmt.Errorf("the refusal does not name the holder: %q", cond.Message)
		}
		return nil
	})

	// The label names the winner, pinned to that object rather than to its
	// name, and only one machine ever wrote it.
	dv := &cdiv1.DataVolume{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "contested"}, dv); err != nil {
		t.Fatalf("reading the contested disk: %v", err)
	}
	holder := dv.Labels[attachedToLabel]
	if holder != "racer-a" && holder != "racer-b" {
		t.Fatalf("attached-to = %q, want one of the two racers", holder)
	}
	winner := getVM(t, ns, holder)
	if dv.Labels[attachedToUIDLabel] != string(winner.UID) {
		t.Fatalf("the claim is not pinned to the winning object: %q vs %q",
			dv.Labels[attachedToUIDLabel], winner.UID)
	}

	// The loser stays refused rather than eventually taking the disk: a race
	// that resolves and then un-resolves is worse than one that never did.
	loser := "racer-a"
	if holder == "racer-a" {
		loser = "racer-b"
	}
	consistently(t, "the loser to stay out", 6*time.Second, func() error {
		kvm, err := getKubeVirtVM(ns, loser)
		if err != nil {
			return err
		}
		for _, v := range kvm.Spec.Template.Spec.Volumes {
			if v.Name == "contested" {
				return fmt.Errorf("the machine that lost the race took the disk anyway")
			}
		}
		fresh := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "contested"}, fresh); err != nil {
			return err
		}
		if fresh.Labels[attachedToLabel] != holder {
			return fmt.Errorf("the holder changed to %q", fresh.Labels[attachedToLabel])
		}
		return nil
	})
}

// A machine must not be able to free a disk it does not hold — a release that
// skips the check hands somebody else's disk away, which is the very outcome
// the claim exists to prevent.
func TestOnlyTheHolderCanReleaseADisk(t *testing.T) {
	ns := "vm-disk-release-guard"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustDataVolume(t, ns, "held-tight")

	holder := newManagedVM(ns, "holder", "ubuntu")
	holder.Spec.Disks = []platformv1alpha1.DiskAttachment{{Claim: "held-tight"}}
	if err := k8sClient.Create(testCtx, holder); err != nil {
		t.Fatalf("creating holder: %v", err)
	}
	eventually(t, "the disk to be held", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "held-tight"}, dv); err != nil {
			return err
		}
		if dv.Labels[attachedToLabel] != "holder" {
			return fmt.Errorf("attached-to = %q", dv.Labels[attachedToLabel])
		}
		return nil
	})

	// Another machine lists the disk, is refused, and is then deleted. Its
	// cleanup must not free a disk it never had.
	other := newManagedVM(ns, "bystander", "ubuntu")
	other.Spec.Disks = []platformv1alpha1.DiskAttachment{{Claim: "held-tight"}}
	if err := k8sClient.Create(testCtx, other); err != nil {
		t.Fatalf("creating bystander: %v", err)
	}
	eventually(t, "the bystander to be refused", func() error {
		got := getVM(t, ns, "bystander")
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionDisksAttached)
		if cond == nil || cond.Status != metav1.ConditionFalse {
			return fmt.Errorf("condition = %+v", cond)
		}
		return nil
	})
	if err := k8sClient.Delete(testCtx, getVM(t, ns, "bystander")); err != nil {
		t.Fatalf("deleting bystander: %v", err)
	}

	eventually(t, "the bystander to be gone", func() error {
		got := &platformv1alpha1.ManagedVM{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "bystander"}, got)
		if apierrors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return err
		}
		return fmt.Errorf("still present")
	})

	dv := &cdiv1.DataVolume{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "held-tight"}, dv); err != nil {
		t.Fatalf("reading the disk: %v", err)
	}
	if dv.Labels[attachedToLabel] != "holder" {
		t.Fatalf("a machine that never held the disk released it: attached-to = %q",
			dv.Labels[attachedToLabel])
	}
}

// The dangerous ordering: a machine's release runs late, after the disk has
// already been claimed by somebody else. Comparing names alone would free the
// new holder's disk; the UID is what makes the check exact.
func TestALateReleaseDoesNotFreeSomebodyElsesClaim(t *testing.T) {
	ns := "vm-disk-late-release"
	mustNamespace(t, ns, "opdev")
	readyImage(t, ns, "ubuntu")
	mustDataVolume(t, ns, "passed-on")

	first := newManagedVM(ns, "first-owner", "ubuntu")
	first.Spec.Disks = []platformv1alpha1.DiskAttachment{{Claim: "passed-on"}}
	if err := k8sClient.Create(testCtx, first); err != nil {
		t.Fatalf("creating the first owner: %v", err)
	}
	eventually(t, "the first owner to hold the disk", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "passed-on"}, dv); err != nil {
			return err
		}
		if dv.Labels[attachedToLabel] != "first-owner" {
			return fmt.Errorf("attached-to = %q", dv.Labels[attachedToLabel])
		}
		return nil
	})

	// Somebody else takes the disk — the same name reused, or simply the next
	// machine along. Either way the claim now belongs to a different object.
	const newOwnerUID = "99999999-8888-7777-6666-555555555555"
	eventually(t, "the disk to be re-claimed by another object", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "passed-on"}, dv); err != nil {
			return err
		}
		dv.Labels[attachedToLabel] = "first-owner"
		dv.Labels[attachedToUIDLabel] = newOwnerUID
		return k8sClient.Update(testCtx, dv)
	})

	// Now the original machine goes away, and its cleanup runs late.
	if err := k8sClient.Delete(testCtx, getVM(t, ns, "first-owner")); err != nil {
		t.Fatalf("deleting the first owner: %v", err)
	}
	eventually(t, "the first owner to be gone", func() error {
		got := &platformv1alpha1.ManagedVM{}
		err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "first-owner"}, got)
		if apierrors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return err
		}
		return fmt.Errorf("still held by finalizers %v", got.Finalizers)
	})

	dv := &cdiv1.DataVolume{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "passed-on"}, dv); err != nil {
		t.Fatalf("reading the disk: %v", err)
	}
	if dv.Labels[attachedToUIDLabel] != newOwnerUID {
		t.Fatalf("a late release freed a claim that had moved on: uid = %q, want %q",
			dv.Labels[attachedToUIDLabel], newOwnerUID)
	}
	if dv.Labels[attachedToLabel] == "" {
		t.Fatal("a late release cleared the holder entirely")
	}
}

// The previous test shows exactly one machine ends up with the disk, but it
// would pass just as well with a plain read-then-write, because the reconciles
// are serialised and the second one simply reads a label that is already there.
// What follows tests the property that makes the outcome safe when they are not
// serialised: the claim write is version-checked, so a writer holding a stale
// read cannot overwrite a claim made in the meantime.
func TestTheClaimWriteIsVersionCheckedNotLastWriterWins(t *testing.T) {
	ns := "vm-claim-cas"
	mustNamespace(t, ns, "opdev")
	mustDataVolume(t, ns, "cas-disk")

	// Reads go through the manager's cache, which trails a create.
	stale := &cdiv1.DataVolume{}
	eventually(t, "the disk to be visible", func() error {
		return k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "cas-disk"}, stale)
	})

	// Somebody else claims it after that read.
	winner := stale.DeepCopy()
	winner.Labels[attachedToLabel] = "machine-b"
	winner.Labels[attachedToUIDLabel] = "bbbbbbbb-0000-0000-0000-000000000000"
	if err := k8sClient.Update(testCtx, winner); err != nil {
		t.Fatalf("first claim: %v", err)
	}

	// The holder of the stale read now tries to claim the same disk. Its object
	// still carries the resourceVersion from before, which is what the API
	// server checks.
	stale.Labels[attachedToLabel] = "machine-a"
	stale.Labels[attachedToUIDLabel] = "aaaaaaaa-0000-0000-0000-000000000000"
	err := k8sClient.Update(testCtx, stale)
	if err == nil {
		t.Fatal("a stale write took the claim; the write is last-writer-wins, not compare-and-set")
	}
	if !apierrors.IsConflict(err) {
		t.Fatalf("expected a conflict, got %v", err)
	}

	eventually(t, "the first claim to stand", func() error {
		after := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "cas-disk"}, after); err != nil {
			return err
		}
		if after.Labels[attachedToLabel] != "machine-b" {
			return fmt.Errorf("holder = %q, want the machine that got there first",
				after.Labels[attachedToLabel])
		}
		return nil
	})
}

// And the same thing with real contention: N callers going for one disk at the
// same moment, exactly one of which may come away with it.
//
// This drives claimDisk directly rather than through Reconcile, because
// contention is the property under test and the controller runs one worker per
// object — the end-to-end outcome is covered by the racing-machines test above.
func TestConcurrentClaimsProduceExactlyOneWinner(t *testing.T) {
	ns := "vm-claim-concurrent"
	mustNamespace(t, ns, "opdev")

	reconciler := &ManagedVMReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}

	for round := range 5 {
		disk := fmt.Sprintf("hot-disk-%d", round)
		mustDataVolume(t, ns, disk)
		// The cache has to know about the disk before the claimants read it,
		// or they all miss it and none of them claims anything.
		eventually(t, "the disk to be visible", func() error {
			dv := &cdiv1.DataVolume{}
			return k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: disk}, dv)
		})

		const claimants = 2
		start := make(chan struct{})
		results := make(chan struct {
			holder string
			err    error
		}, claimants)

		var wg sync.WaitGroup
		for i := range claimants {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				<-start
				holder, err := reconciler.claimDisk(
					testCtx, ns, disk,
					fmt.Sprintf("claimant-%d", i),
					fmt.Sprintf("%08d-0000-0000-0000-000000000000", i),
				)
				results <- struct {
					holder string
					err    error
				}{holder, err}
			}(i)
		}
		close(start)
		wg.Wait()
		close(results)

		won := 0
		for res := range results {
			switch {
			case res.err != nil:
				// Lost on a conflict: the API server refused the stale write.
				if !contains(res.err.Error(), "lost the race") {
					t.Fatalf("round %d: unexpected error %v", round, res.err)
				}
			case res.holder != "":
				// Lost on a read: somebody already held it.
			default:
				won++
			}
		}
		if won != 1 {
			t.Fatalf("round %d: %d claimants believe they hold the disk; exactly one may", round, won)
		}

		eventually(t, fmt.Sprintf("round %d: the winner's claim to be visible", round), func() error {
			dv := &cdiv1.DataVolume{}
			if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: disk}, dv); err != nil {
				return err
			}
			if dv.Labels[attachedToLabel] == "" {
				return fmt.Errorf("a winner was reported but the disk has no holder yet")
			}
			return nil
		})
	}
}

// staleReader hands out a snapshot taken earlier for one named object, and
// passes everything else through.
//
// It exists to force the case the concurrent test can only hope for: a claimant
// whose read happened *before* somebody else's claim landed, so its write
// carries an out-of-date resourceVersion. Without this the loser usually loses
// on the read, and the compare-and-set branch is never exercised — a test that
// would pass just as well if the write were last-writer-wins.
type staleReader struct {
	client.Client
	name     string
	snapshot *cdiv1.DataVolume
}

func (s staleReader) Get(
	ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption,
) error {
	if dv, ok := obj.(*cdiv1.DataVolume); ok && key.Name == s.name {
		s.snapshot.DeepCopyInto(dv)
		return nil
	}
	return s.Client.Get(ctx, key, obj, opts...)
}

// The claim must be decided by the API server's version check, not by whoever
// writes last.
func TestAClaimantWithAStaleReadLosesOnConflict(t *testing.T) {
	ns := "vm-claim-forced"
	mustNamespace(t, ns, "opdev")
	mustDataVolume(t, ns, "forced-disk")

	snapshot := &cdiv1.DataVolume{}
	eventually(t, "the disk to be visible", func() error {
		return k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "forced-disk"}, snapshot)
	})

	winner := &ManagedVMReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
	holder, err := winner.claimDisk(testCtx, ns, "forced-disk", "machine-b",
		"bbbbbbbb-0000-0000-0000-000000000000")
	if err != nil {
		t.Fatalf("first claim: %v", err)
	}
	if holder != "" {
		t.Fatalf("the disk was already held by %q before the test started", holder)
	}

	// Now a claimant that read the disk before that claim landed. Its write
	// carries the old resourceVersion, which is the whole question.
	loser := &ManagedVMReconciler{
		Client: staleReader{Client: k8sClient, name: "forced-disk", snapshot: snapshot},
		Scheme: k8sClient.Scheme(),
	}
	holder, err = loser.claimDisk(testCtx, ns, "forced-disk", "machine-a",
		"aaaaaaaa-0000-0000-0000-000000000000")
	if err == nil {
		t.Fatalf("a stale claimant succeeded (holder=%q); the write is not version-checked", holder)
	}
	if !contains(err.Error(), "lost the race") {
		t.Fatalf("expected the conflict to be reported as a lost race, got %v", err)
	}
	if !apierrors.IsConflict(errors.Unwrap(err)) {
		t.Fatalf("expected a conflict underneath, got %v", errors.Unwrap(err))
	}

	// And the disk still belongs to whoever got there first.
	eventually(t, "the first claim to stand", func() error {
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: "forced-disk"}, dv); err != nil {
			return err
		}
		if dv.Labels[attachedToLabel] != "machine-b" {
			return fmt.Errorf("holder = %q", dv.Labels[attachedToLabel])
		}
		if dv.Labels[attachedToUIDLabel] != "bbbbbbbb-0000-0000-0000-000000000000" {
			return fmt.Errorf("holder uid = %q", dv.Labels[attachedToUIDLabel])
		}
		return nil
	})

	// The loser, reading afresh, is told who has it rather than taking it.
	holder, err = winner.claimDisk(testCtx, ns, "forced-disk", "machine-a",
		"aaaaaaaa-0000-0000-0000-000000000000")
	if err != nil {
		t.Fatalf("re-reading claimant: %v", err)
	}
	if holder != "machine-b" {
		t.Fatalf("holder reported as %q, want machine-b", holder)
	}
}
