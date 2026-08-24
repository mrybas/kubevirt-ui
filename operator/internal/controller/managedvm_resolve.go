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
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kubevirt"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
	"github.com/mrybas/kubevirt-ui/operator/internal/scope"
)

// blocked is a reason the VM cannot be rendered yet, phrased for a human.
//
// It is a value rather than an error because none of these are failures of the
// controller: they are states of the world that the controller reports and
// waits out. A missing image is not a bad request — apply order is not part of
// this API.
type blocked struct {
	Reason  string
	Message string
	// Fatal marks a state that will not fix itself, so there is no point
	// retrying on a timer; only an edit will change it.
	Fatal bool
}

func (b *blocked) Error() string { return b.Reason + ": " + b.Message }

// legacyTemplate is the shape stored in the kubevirt-ui-templates ConfigMap.
//
// Templates become a custom resource of their own later; until then the
// controller reads the store the UI already writes, so both paths see one set
// of templates rather than two that drift.
type legacyTemplate struct {
	DisplayName          string `json:"display_name"`
	OSType               string `json:"os_type"`
	GoldenImageName      string `json:"golden_image_name"`
	GoldenImageNamespace string `json:"golden_image_namespace"`
	Compute              struct {
		CPUCores   int32  `json:"cpu_cores"`
		CPUSockets int32  `json:"cpu_sockets"`
		CPUThreads int32  `json:"cpu_threads"`
		Memory     string `json:"memory"`
	} `json:"compute"`
	Disk struct {
		Size string `json:"size"`
		// storage_class is read but deliberately not applied: templates live on
		// a cheap erasure-coded class and VM disks belong on the replicated
		// one. Inheriting it sent every write a VM made to erasure coding.
		StorageClass *string `json:"storage_class"`
	} `json:"disk"`
	CloudInit *struct {
		UserData string `json:"user_data"`
	} `json:"cloud_init"`
	Console *struct {
		VNCEnabled           *bool `json:"vnc_enabled"`
		SerialConsoleEnabled *bool `json:"serial_console_enabled"`
	} `json:"console"`
}

const (
	systemNamespace      = "kubevirt-ui-system"
	templatesConfigMap   = "kubevirt-ui-templates"
	settingsConfigMap    = "kubevirt-ui-settings"
	vpcDNSConfigMap      = "vpc-dns-config"
	kubeOVNSystemVPCName = "ovn-cluster"
)

var (
	subnetGVK = schema.GroupVersionKind{Group: "kubeovn.io", Version: "v1", Kind: "Subnet"}
	vpcGVK    = schema.GroupVersionKind{Group: "kubeovn.io", Version: "v1", Kind: "Vpc"}
)

// resolveSource fills in the image and the compute/disk defaults, from either a
// template or a direct image reference.
func (r *ManagedVMReconciler) resolveSource(
	ctx context.Context, vm *platformv1alpha1.ManagedVM, in *kubevirt.Input,
) *blocked {
	if vm.Spec.TemplateRef != nil {
		return r.resolveTemplate(ctx, vm, in)
	}
	return r.resolveImage(ctx, vm, vm.Spec.ImageRef, in)
}

// resolveTemplate prefers the custom resource and falls back to the legacy
// store.
//
// Both are read during the migration so that templates written either way are
// usable from either path — the alternative is two sets of templates that drift
// and a user who cannot tell which picker they are looking at. The fallback
// goes away one release after the migration script has run.
func (r *ManagedVMReconciler) resolveTemplate(
	ctx context.Context, vm *platformv1alpha1.ManagedVM, in *kubevirt.Input,
) *blocked {
	tpl := &platformv1alpha1.ManagedVMTemplate{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: vm.Namespace, Name: vm.Spec.TemplateRef.Name,
	}, tpl)
	if err == nil {
		return r.applyTemplateResource(ctx, vm, tpl, in)
	}
	if !apierrors.IsNotFound(err) {
		return &blocked{Reason: "TemplateUnreadable", Message: err.Error()}
	}
	return r.resolveLegacyTemplate(ctx, vm, in)
}

// applyTemplateResource fills the defaults from a ManagedVMTemplate and
// resolves the image it names.
func (r *ManagedVMReconciler) applyTemplateResource(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	tpl *platformv1alpha1.ManagedVMTemplate,
	in *kubevirt.Input,
) *blocked {
	in.TemplateName = tpl.Name
	in.Cores = tpl.Spec.Compute.Cores
	in.Sockets = tpl.Spec.Compute.Sockets
	in.Threads = tpl.Spec.Compute.Threads
	in.Memory = tpl.Spec.Compute.Memory
	in.DiskSize = tpl.Spec.RootDisk.Size
	if tpl.Spec.CloudInit != nil {
		in.CloudInitUserData = tpl.Spec.CloudInit.UserData
	}
	in.VNC, in.Serial = true, false
	if c := tpl.Spec.Console; c != nil {
		if c.VNC != nil {
			in.VNC = *c.VNC
		}
		if c.Serial != nil {
			in.Serial = *c.Serial
		}
	}
	// The image goes through the same resolution a direct reference does,
	// including the readiness gate and the cross-namespace scope check — a
	// template is a convenience, not a way around either.
	ref := tpl.Spec.ImageRef
	if ref.Namespace == "" {
		ref.Namespace = tpl.Namespace
	}
	return r.resolveImage(ctx, vm, &ref, in)
}

func (r *ManagedVMReconciler) resolveLegacyTemplate(
	ctx context.Context, vm *platformv1alpha1.ManagedVM, in *kubevirt.Input,
) *blocked {
	cm := &corev1.ConfigMap{}
	err := r.Get(ctx, types.NamespacedName{Namespace: systemNamespace, Name: templatesConfigMap}, cm)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return &blocked{
				Reason:  "TemplateStoreMissing",
				Message: fmt.Sprintf("ConfigMap %s/%s does not exist", systemNamespace, templatesConfigMap),
			}
		}
		return &blocked{Reason: "TemplateStoreUnreadable", Message: err.Error()}
	}

	raw, ok := cm.Data[vm.Spec.TemplateRef.Name]
	if !ok {
		return &blocked{
			Reason:  "TemplateNotFound",
			Message: fmt.Sprintf("template %q is not in %s/%s", vm.Spec.TemplateRef.Name, systemNamespace, templatesConfigMap),
		}
	}

	var tpl legacyTemplate
	if err := json.Unmarshal([]byte(raw), &tpl); err != nil {
		return &blocked{
			Reason:  "TemplateUnreadable",
			Message: fmt.Sprintf("template %q is not valid JSON: %v", vm.Spec.TemplateRef.Name, err),
			Fatal:   true,
		}
	}
	if tpl.GoldenImageName == "" {
		return &blocked{
			Reason:  "TemplateHasNoImage",
			Message: fmt.Sprintf("template %q configures no golden image", vm.Spec.TemplateRef.Name),
			Fatal:   true,
		}
	}

	in.TemplateName = vm.Spec.TemplateRef.Name
	in.Cores, in.Sockets, in.Threads = tpl.Compute.CPUCores, tpl.Compute.CPUSockets, tpl.Compute.CPUThreads
	in.Memory = tpl.Compute.Memory
	in.DiskSize = tpl.Disk.Size
	if tpl.CloudInit != nil {
		in.CloudInitUserData = tpl.CloudInit.UserData
	}
	in.VNC, in.Serial = true, false
	if tpl.Console != nil {
		if tpl.Console.VNCEnabled != nil {
			in.VNC = *tpl.Console.VNCEnabled
		}
		if tpl.Console.SerialConsoleEnabled != nil {
			in.Serial = *tpl.Console.SerialConsoleEnabled
		}
	}

	// A legacy template points at a DataVolume by its generated name. That name
	// is also the claim's name, which is all the clone needs.
	in.GoldenPVCName = tpl.GoldenImageName
	in.GoldenPVCNamespace = tpl.GoldenImageNamespace
	if in.GoldenPVCNamespace == "" {
		in.GoldenPVCNamespace = vm.Namespace
	}
	return nil
}

func (r *ManagedVMReconciler) resolveImage(
	ctx context.Context,
	vm *platformv1alpha1.ManagedVM,
	ref *platformv1alpha1.ImageRef,
	in *kubevirt.Input,
) *blocked {
	ns := ref.Namespace
	if ns == "" {
		ns = vm.Namespace
	}

	img := &platformv1alpha1.ManagedImage{}
	err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: ref.Name}, img)
	if err != nil {
		if apierrors.IsNotFound(err) {
			// Not an admission error and not a permanent one: one apply may
			// legitimately create the image and the VM together, in any order.
			return &blocked{
				Reason:  "ImageNotFound",
				Message: fmt.Sprintf("ManagedImage %s/%s does not exist yet", ns, ref.Name),
			}
		}
		return &blocked{Reason: "ImageUnreadable", Message: err.Error()}
	}

	if ns != vm.Namespace {
		if b := r.checkImageScope(ctx, vm, img); b != nil {
			return b
		}
	}

	if img.Status.Phase != platformv1alpha1.ImagePhaseReady {
		return &blocked{
			Reason: "ImageNotReady",
			Message: fmt.Sprintf("ManagedImage %s/%s is %s; cloning from an unfinished disk produces a broken VM",
				ns, ref.Name, orUnknown(img.Status.Phase)),
		}
	}

	claim := img.Status.DataVolumeName
	if claim == "" {
		claim = img.Name
	}
	in.GoldenPVCName = claim
	in.GoldenPVCNamespace = ns
	in.VNC, in.Serial = true, false
	return nil
}

// checkImageScope decides whether this VM may clone an image from another
// namespace.
//
// It is re-evaluated on every pass, not only at admission, because the
// privileged act happens later and can happen again: an image's scope can be
// narrowed after the VM was accepted, and a root disk may be provisioned again
// months afterwards.
func (r *ManagedVMReconciler) checkImageScope(
	ctx context.Context, vm *platformv1alpha1.ManagedVM, img *platformv1alpha1.ManagedImage,
) *blocked {
	scope := img.Spec.Scope
	if scope == "" {
		scope = "environment"
	}
	if scope == "environment" {
		return &blocked{
			Reason: "ImageAccessDenied",
			Message: fmt.Sprintf("image %s/%s has scope=environment, so it is private to its own namespace",
				img.Namespace, img.Name),
			Fatal: true,
		}
	}

	vmNS, imgNS := &corev1.Namespace{}, &corev1.Namespace{}
	if err := r.Get(ctx, types.NamespacedName{Name: vm.Namespace}, vmNS); err != nil {
		return &blocked{Reason: "NamespaceUnreadable", Message: err.Error()}
	}
	if err := r.Get(ctx, types.NamespacedName{Name: img.Namespace}, imgNS); err != nil {
		return &blocked{Reason: "NamespaceUnreadable", Message: err.Error()}
	}

	switch scope {
	case "project":
		a, b := vmNS.Labels[naming.ProjectLabel], imgNS.Labels[naming.ProjectLabel]
		if a == "" || a != b {
			return &blocked{
				Reason: "ImageAccessDenied",
				Message: fmt.Sprintf("image %s/%s is project-scoped to %q and this VM is in project %q",
					img.Namespace, img.Name, orUnknown(b), orUnknown(a)),
				Fatal: true,
			}
		}
	case "folder":
		a, b := vmNS.Labels[naming.FolderLabel], imgNS.Labels[naming.FolderLabel]
		if a == "" || a != b {
			return &blocked{
				Reason: "ImageAccessDenied",
				Message: fmt.Sprintf("image %s/%s is folder-scoped to %q and this VM is in folder %q",
					img.Namespace, img.Name, orUnknown(b), orUnknown(a)),
				Fatal: true,
			}
		}
	}
	return nil
}

// resolveNetworks looks each subnet up and decides how the NIC attaches.
//
// A subnet that cannot be read is reported, not assumed. The handler this
// replaces treated a failed lookup as "it must be a VLAN then", which turns a
// typo into a VM wired to a network attachment that does not exist — and the
// only symptom is a guest with no address.
func (r *ManagedVMReconciler) resolveNetworks(
	ctx context.Context, vm *platformv1alpha1.ManagedVM, in *kubevirt.Input,
) *blocked {
	if len(vm.Spec.Networks) == 0 {
		return nil
	}

	ns := &corev1.Namespace{}
	if err := r.Get(ctx, types.NamespacedName{Name: vm.Namespace}, ns); err != nil {
		return &blocked{Reason: "NamespaceUnreadable", Message: err.Error()}
	}
	target := scope.Target{
		Folder:      ns.Labels[naming.FolderLabel],
		Environment: ns.Labels[naming.EnvironmentLabel],
	}

	for idx, nic := range vm.Spec.Networks {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := r.Get(ctx, types.NamespacedName{Name: nic.Subnet}, subnet); err != nil {
			if apierrors.IsNotFound(err) {
				return &blocked{
					Reason:  "NetworkNotFound",
					Message: fmt.Sprintf("subnet %q does not exist", nic.Subnet),
				}
			}
			return &blocked{Reason: "NetworkUnreadable", Message: err.Error()}
		}

		vlan, _, _ := unstructured.NestedString(subnet.Object, "spec", "vlan")
		vpc, _, _ := unstructured.NestedString(subnet.Object, "spec", "vpc")
		isOverlay := vlan == "" && vpc != "" && vpc != kubeOVNSystemVPCName

		// Only the primary NIC may sit on a VPC overlay: a secondary would need
		// a per-subnet attachment definition wrapping the OVN CNI, which does
		// not exist. This is checked here as well as at admission, because a
		// subnet can change from underlay to overlay after the VM was accepted.
		if isOverlay && idx != 0 {
			return &blocked{
				Reason: "VPCMustBePrimary",
				Message: fmt.Sprintf("subnet %q is a VPC overlay and can only be the first NIC; "+
					"use a VLAN-backed subnet for additional interfaces", nic.Subnet),
				Fatal: true,
			}
		}

		if res := scope.Check(r.networkScope(ctx, subnet, vpc, vlan, nic.Subnet), target); !res.Allowed {
			return &blocked{Reason: res.Reason, Message: res.Message, Fatal: true}
		}

		in.Networks = append(in.Networks, kubevirt.ResolvedNetwork{
			Subnet:       nic.Subnet,
			VLAN:         vlan,
			IsVPCOverlay: isOverlay,
			StaticIP:     nic.StaticIP,
		})

		if isOverlay && in.VPCDNSVIP == "" {
			in.VPCDNSVIP = r.vpcDNSVIP(ctx, subnet)
		}
	}
	return nil
}

// resolvableCondition says whether this machine has a name server it can reach.
//
// A machine on the default overlay uses the cluster's resolver and that works,
// so there is nothing to report. A machine on a VPC is a different story: with
// bridge binding the guest is served DHCP by its own launcher pod and gets
// that pod's resolver, which is the cluster CoreDNS ClusterIP — no route from
// inside a VPC. The subnet's own `dhcpV4Options` never reach the guest at all,
// which is the part that took a packet capture inside a VM to establish.
//
// So the machine either has a resolver placed on the launcher deliberately, or
// it has none. "None" is a legitimate state; looking identical to "configured"
// is not, and that is what it did — an IP answers, a name does not, and no
// object anywhere mentions DNS.
func resolvableCondition(
	vm *platformv1alpha1.ManagedVM, in kubevirt.Input,
) metav1.Condition {
	onVPC := false
	for _, n := range in.Networks {
		if n.IsVPCOverlay {
			onVPC = true
			break
		}
	}
	switch {
	case !onVPC:
		return metav1.Condition{
			Type: platformv1alpha1.ConditionResolvable, Status: metav1.ConditionTrue,
			Reason:             "ClusterDNS",
			Message:            "this machine is on the cluster overlay and uses the cluster resolver",
			ObservedGeneration: vm.Generation,
		}
	case in.VPCDNSVIP != "":
		return metav1.Condition{
			Type: platformv1alpha1.ConditionResolvable, Status: metav1.ConditionTrue,
			Reason:             "VPCResolver",
			Message:            "the guest resolves names at " + in.VPCDNSVIP,
			ObservedGeneration: vm.Generation,
		}
	default:
		return metav1.Condition{
			Type: platformv1alpha1.ConditionResolvable, Status: metav1.ConditionFalse,
			Reason: "NoResolverInVPC",
			Message: "this machine is on a VPC and has no name server it can " +
				"reach: kube-ovn's vpc-dns is not enabled and the network " +
				"names no dnsServer, so the launcher hands the guest the " +
				"cluster resolver, which has no route from here. Addresses " +
				"work and names do not. Enable vpc-dns in kube-ovn, or set a " +
				"dnsServer on the network that is reachable from it.",
			ObservedGeneration: vm.Generation,
		}
	}
}

// vpcDNSVIP finds the resolver a guest on this subnet must use.
//
// It reads the address the datapath actually hands out — the subnet's own DHCP
// options — and falls back to kube-ovn's vpc-dns configuration. Neither is a
// formula: deriving the address from the service CIDR happens to give the right
// answer on this cluster and is a guess on any other.
func (r *ManagedVMReconciler) vpcDNSVIP(ctx context.Context, subnet *unstructured.Unstructured) string {
	opts, _, _ := unstructured.NestedString(subnet.Object, "spec", "dhcpV4Options")
	for _, part := range strings.Split(opts, ",") {
		k, v, found := strings.Cut(strings.TrimSpace(part), "=")
		if found && k == "dns_server" && v != "" {
			return v
		}
	}

	for _, ns := range r.kubeOVNNamespaces() {
		cm := &corev1.ConfigMap{}
		if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: vpcDNSConfigMap}, cm); err == nil {
			if vip := cm.Data["coredns-vip"]; vip != "" {
				return vip
			}
		}
	}
	// No VIP found. The guest keeps the cluster resolver, which is what shipped;
	// the caller records that on the VM rather than pretending it is configured.
	return ""
}

// networkScope gathers what the scope rule needs. Folder and environment are
// read from the VPC, which is where the wizard reads them too — the subnet
// carries its own copy, and trusting that copy would mean two sources for one
// fact.
func (r *ManagedVMReconciler) networkScope(
	ctx context.Context,
	subnet *unstructured.Unstructured,
	vpc, vlan, name string,
) scope.Network {
	net := scope.Network{
		Name:    name,
		VPC:     vpc,
		VLAN:    vlan,
		Purpose: subnet.GetLabels()[scope.PurposeLabel],
	}
	if vpc == "" || vpc == kubeOVNSystemVPCName {
		return net
	}

	vpcObj := &unstructured.Unstructured{}
	vpcObj.SetGroupVersionKind(vpcGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: vpc}, vpcObj); err != nil {
		// The VPC cannot be read, so its scope is unknown. Treating unknown as
		// global would hand out another folder's network on a transient error;
		// the subnet's own labels are the conservative fallback.
		net.Folder = subnet.GetLabels()[naming.FolderLabel]
		net.Environment = subnet.GetLabels()[naming.EnvironmentLabel]
		return net
	}
	net.Folder = vpcObj.GetLabels()[naming.FolderLabel]
	net.Environment = vpcObj.GetLabels()[naming.EnvironmentLabel]
	return net
}

func orUnknown(s string) string {
	if s == "" {
		return "unknown"
	}
	return s
}
