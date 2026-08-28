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

// Package kubevirt renders a ManagedVM into the VirtualMachine KubeVirt runs.
//
// Everything in here used to live inside one HTTP handler, which is why none of
// it applied to anyone writing manifests directly: the overcommit arithmetic,
// the network-data that keeps a restored guest from losing its NIC, the VPC
// resolver, the annotations kube-ovn needs. Rendering it in a controller is
// what makes the rules the same for the UI and for a Terraform module.
package kubevirt

import (
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/naming"
)

// GuestNetworkData is the netplan handed to the guest, matching the NIC by name.
//
// Left alone, cloud-init pins netplan to the MAC it saw on first boot. KubeVirt
// generates that MAC when the VMI starts and keeps it nowhere the VM object
// carries, so a VM restored from a backup boots with a different one and the
// guest has an interface it does not recognise — no address, no DNS, while the
// backup reports Completed and the VM page shows an IP the guest never used.
// Matching on the name makes the guest indifferent to the MAC, which is the
// property actually wanted; pinning the MAC into the spec instead would hand
// every clone a duplicate address on the same subnet.
//
// optional: true keeps a NIC without carrier from holding the boot for two
// minutes — cloud-init's own data comes from the attached disk, not the network.
const GuestNetworkData = `version: 2
ethernets:
  primary:
    match:
      name: "e*"
    dhcp4: true
    dhcp6: false
    optional: true
`

const (
	rootDiskVolume  = "rootdisk"
	cloudInitVolume = "cloudinit"

	// Annotations kube-ovn and KubeVirt read off the launcher pod template.
	logicalSwitchAnnotation = "ovn.kubernetes.io/logical_switch"
	ipAddressAnnotation     = "ovn.kubernetes.io/ip_address"
	bridgeMigrationAnno     = "kubevirt.io/allow-pod-bridge-network-live-migration"

	// Labels and annotations the product's own listers read.
	appLabel        = "app"
	templateLabel   = "kubevirt-ui.io/template"
	vmDiskLabel     = "kubevirt-ui.io/vm-disk"
	vmNameLabel     = "kubevirt-ui.io/vm-name"
	ownerAnnotation = "kubevirt-ui.io/owner"
)

// ResolvedNetwork is one NIC after the subnet has been looked up.
type ResolvedNetwork struct {
	// Subnet is the kube-ovn subnet name, as asked for.
	Subnet string
	// VLAN is the subnet's VLAN when it is underlay-backed, empty for a VPC
	// overlay. This is read from the Subnet object, never guessed: a lookup
	// that fails is a condition on the VM, not an assumption that it must have
	// been a VLAN.
	VLAN string
	// IsVPCOverlay is true for a subnet belonging to a VPC other than the
	// cluster's own.
	IsVPCOverlay bool
	// StaticIP pins the address.
	StaticIP string
}

// Input is everything the renderer needs, already resolved.
//
// The controller does the looking-up; this function does no I/O so that what it
// produces can be compared field by field in a test without a cluster.
type Input struct {
	VM *platformv1alpha1.ManagedVM

	// Compute, after template defaults have been applied.
	Cores, Sockets, Threads int32
	Memory                  string

	// Root disk, after template defaults have been applied.
	DiskSize     string
	StorageClass string
	RootDiskName string

	// GoldenPVCName/Namespace is the claim the root disk is cloned from.
	GoldenPVCName      string
	GoldenPVCNamespace string

	// GoldenDataSourceName/Namespace is the image's published DataSource, when
	// it has one. Preferred over the claim: the DataSource is where the image
	// says what clones should come from, and it can move from the claim to a
	// permanent snapshot — which costs half the storage operations per
	// machine — without a single consumer changing.
	//
	// Empty for a legacy template, which names a DataVolume directly and has no
	// ManagedImage behind it to publish anything.
	GoldenDataSourceName      string
	GoldenDataSourceNamespace string

	// CPUOvercommit is the cluster-wide ratio; 1 or 0 means none.
	CPUOvercommit int

	// CloudInitUserData is the merged document, or empty for none.
	CloudInitUserData string

	// VNC and Serial decide which consoles are attached.
	VNC, Serial bool

	// Networks in order; the first is the primary.
	Networks []ResolvedNetwork

	// VPCDNSVIP is the resolver a VPC-attached guest must use, or empty to
	// leave the guest on cluster DNS.
	VPCDNSVIP string

	// Project and Environment come from the namespace's labels.
	Project, Environment string

	// TemplateName labels the VM with the template it came from, if any.
	TemplateName string

	// Owner is the human the UI recorded as having asked for this VM.
	Owner string
}

// Labels are the VM's labels, recomputed on every pass.
//
// They are reconciled rather than stamped once: the project and environment
// labels are copied from the namespace, and a VM created before its namespace
// was labelled currently keeps the gap forever, which makes it invisible to
// every folder-scoped view in the product.
func Labels(in Input) map[string]string {
	slugSeed := in.VM.Spec.DisplayName
	if slugSeed == "" {
		slugSeed = in.VM.Name
	}
	labels := map[string]string{
		appLabel:            naming.Slug(slugSeed),
		naming.ManagedLabel: "true",
		naming.SlugLabel:    naming.Slug(slugSeed),
		// Unique per VM, unlike the slug: backup selection targets this, and it
		// is stamped here rather than patched in after create, where a failure
		// was swallowed and left the VM unselectable.
		vmNameLabel: in.VM.Name,

		naming.OwnerUIDLabel:  string(in.VM.UID),
		naming.OwnerNameLabel: in.VM.Name,
		naming.OwnerKindLabel: "ManagedVM",
	}
	if in.TemplateName != "" {
		labels[templateLabel] = in.TemplateName
	}
	if in.Project != "" {
		labels[naming.ProjectLabel] = in.Project
	}
	if in.Environment != "" {
		labels[naming.EnvironmentLabel] = in.Environment
	}
	return labels
}

// Annotations are the VM's annotations.
func Annotations(in Input) map[string]string {
	out := map[string]string{}
	if name := in.VM.Spec.DisplayName; name != "" {
		out[naming.DisplayNameAnnotation] = name
	}
	if in.Owner != "" {
		out[ownerAnnotation] = in.Owner
	}
	return out
}

// CPURequest is the scheduler-visible CPU, after overcommit.
//
// Requests are the real cores divided by the ratio, limits are the real cores:
// the guest keeps its full allowance while the scheduler packs by the divided
// figure.
func CPURequest(totalRealCPUs int32, overcommit int) string {
	if overcommit > 1 {
		milli := int(float64(totalRealCPUs) / float64(overcommit) * 1000)
		if milli < 100 {
			milli = 100
		}
		return fmt.Sprintf("%dm", milli)
	}
	return fmt.Sprintf("%d", totalRealCPUs)
}

// RootDiskTemplate renders the dataVolumeTemplates entry that clones the image.
//
// The name is literal because volume references match a DataVolume by exact
// name, so it cannot be generated server-side. It carries an epoch so a disk
// provisioned again — after a rollback, say — cannot collide with a predecessor
// that is still terminating.
func RootDiskTemplate(in Input) (kubevirtv1.DataVolumeTemplateSpec, error) {
	size, err := resource.ParseQuantity(in.DiskSize)
	if err != nil {
		return kubevirtv1.DataVolumeTemplateSpec{},
			fmt.Errorf("root disk size %q is not a quantity: %w", in.DiskSize, err)
	}

	storage := &cdiv1.StorageSpec{
		Resources: corev1.VolumeResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceStorage: size},
		},
	}
	if in.StorageClass != "" {
		sc := in.StorageClass
		storage.StorageClassName = &sc
	}

	return kubevirtv1.DataVolumeTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{
			Name: in.RootDiskName,
			Labels: map[string]string{
				naming.ManagedLabel: "true",
				vmDiskLabel:         "true",
			},
		},
		Spec: rootDiskSource(in, storage),
	}, nil
}

// rootDiskSource points the disk at the image, in whichever form the image
// publishes.
//
// One place decides this for the whole renderer, because the two forms are not
// interchangeable to read: `sourceRef` hands the choice to the ManagedImage,
// `source.pvc` hard-codes the claim here. Anything that guessed per call site
// would eventually guess differently in two of them.
func rootDiskSource(in Input, storage *cdiv1.StorageSpec) cdiv1.DataVolumeSpec {
	if in.GoldenDataSourceName != "" {
		ns := in.GoldenDataSourceNamespace
		return cdiv1.DataVolumeSpec{
			SourceRef: &cdiv1.DataVolumeSourceRef{
				Kind:      "DataSource",
				Name:      in.GoldenDataSourceName,
				Namespace: &ns,
			},
			Storage: storage,
		}
	}
	return cdiv1.DataVolumeSpec{
		Source: &cdiv1.DataVolumeSource{
			PVC: &cdiv1.DataVolumeSourcePVC{
				Name:      in.GoldenPVCName,
				Namespace: in.GoldenPVCNamespace,
			},
		},
		Storage: storage,
	}
}

// VirtualMachine renders the whole object.
func VirtualMachine(in Input) (*kubevirtv1.VirtualMachine, error) {
	rootDisk, err := RootDiskTemplate(in)
	if err != nil {
		return nil, err
	}

	memory, err := resource.ParseQuantity(in.Memory)
	if err != nil {
		return nil, fmt.Errorf("memory %q is not a quantity: %w", in.Memory, err)
	}

	sockets := in.Sockets
	if sockets < 1 {
		sockets = 1
	}
	threads := in.Threads
	if threads < 1 {
		threads = 1
	}
	totalRealCPUs := in.Cores * sockets * threads

	cpuRequest, err := resource.ParseQuantity(CPURequest(totalRealCPUs, in.CPUOvercommit))
	if err != nil {
		return nil, fmt.Errorf("computing cpu request: %w", err)
	}
	cpuLimit, err := resource.ParseQuantity(fmt.Sprintf("%d", totalRealCPUs))
	if err != nil {
		return nil, fmt.Errorf("computing cpu limit: %w", err)
	}

	vnc, serial := in.VNC, in.Serial
	devices := kubevirtv1.Devices{
		Disks: []kubevirtv1.Disk{
			{
				Name:       rootDiskVolume,
				DiskDevice: kubevirtv1.DiskDevice{Disk: &kubevirtv1.DiskTarget{Bus: kubevirtv1.DiskBusVirtio}},
			},
			// The cloud-init disk is attached even with no user data: it carries
			// the network config above. With no datasource at all cloud-init
			// falls back to its own guess and writes a MAC match again.
			{
				Name:       cloudInitVolume,
				DiskDevice: kubevirtv1.DiskDevice{Disk: &kubevirtv1.DiskTarget{Bus: kubevirtv1.DiskBusVirtio}},
			},
		},
		AutoattachGraphicsDevice: &vnc,
		AutoattachSerialConsole:  &serial,
	}

	cloudInit := &kubevirtv1.CloudInitNoCloudSource{NetworkData: GuestNetworkData}
	if in.CloudInitUserData != "" {
		cloudInit.UserData = in.CloudInitUserData
	}

	volumes := []kubevirtv1.Volume{
		{
			Name:         rootDiskVolume,
			VolumeSource: kubevirtv1.VolumeSource{DataVolume: &kubevirtv1.DataVolumeSource{Name: in.RootDiskName}},
		},
		{
			Name:         cloudInitVolume,
			VolumeSource: kubevirtv1.VolumeSource{CloudInitNoCloud: cloudInit},
		},
	}

	networks, interfaces, podAnnotations, needsVPCDNS := renderNetworks(in)
	devices.Interfaces = interfaces

	vmiSpec := kubevirtv1.VirtualMachineInstanceSpec{
		Domain: kubevirtv1.DomainSpec{
			CPU: &kubevirtv1.CPU{
				Cores:   uint32(in.Cores),
				Sockets: uint32(sockets),
				Threads: uint32(threads),
			},
			Memory: &kubevirtv1.Memory{Guest: &memory},
			Resources: kubevirtv1.ResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceCPU:    cpuRequest,
					corev1.ResourceMemory: memory,
				},
				Limits: corev1.ResourceList{
					corev1.ResourceCPU:    cpuLimit,
					corev1.ResourceMemory: memory,
				},
			},
			Devices: devices,
		},
		Networks: networks,
		Volumes:  volumes,
	}

	// With bridge binding KubeVirt hands the guest the launcher pod's resolver,
	// and that pod gets the cluster CoreDNS address — which has no route from
	// inside a VPC. The subnet's own DHCP offers the right resolver and never
	// reaches the guest, because the guest is served by the launcher. So the
	// launcher is told directly.
	if needsVPCDNS && in.VPCDNSVIP != "" {
		vmiSpec.DNSPolicy = corev1.DNSNone
		vmiSpec.DNSConfig = &corev1.PodDNSConfig{
			Nameservers: []string{in.VPCDNSVIP},
			Searches: []string{
				fmt.Sprintf("%s.svc.cluster.local", in.VM.Namespace),
				"svc.cluster.local",
				"cluster.local",
			},
			Options: []corev1.PodDNSConfigOption{{Name: "ndots", Value: ptr("5")}},
		}
	}

	runStrategy := kubevirtv1.RunStrategyHalted
	if in.VM.Spec.Running {
		runStrategy = kubevirtv1.RunStrategyAlways
	}

	templateMeta := metav1.ObjectMeta{}
	if len(podAnnotations) > 0 {
		templateMeta.Annotations = podAnnotations
	}

	return &kubevirtv1.VirtualMachine{
		ObjectMeta: metav1.ObjectMeta{
			Name:        in.VM.Name,
			Namespace:   in.VM.Namespace,
			Labels:      Labels(in),
			Annotations: Annotations(in),
		},
		Spec: kubevirtv1.VirtualMachineSpec{
			RunStrategy:         &runStrategy,
			DataVolumeTemplates: []kubevirtv1.DataVolumeTemplateSpec{rootDisk},
			Template: &kubevirtv1.VirtualMachineInstanceTemplateSpec{
				ObjectMeta: templateMeta,
				Spec:       vmiSpec,
			},
		},
	}, nil
}

// renderNetworks turns resolved subnets into KubeVirt networks, interfaces and
// the annotations kube-ovn reads off the launcher pod.
func renderNetworks(in Input) (
	[]kubevirtv1.Network, []kubevirtv1.Interface, map[string]string, bool,
) {
	annotations := map[string]string{}

	if len(in.Networks) == 0 {
		// No NIC asked for: the default pod network, masqueraded, which is what
		// a VM with no network configuration has always got.
		return []kubevirtv1.Network{*kubevirtv1.DefaultPodNetwork()},
			[]kubevirtv1.Interface{{
				Name:                   "default",
				InterfaceBindingMethod: kubevirtv1.InterfaceBindingMethod{Masquerade: &kubevirtv1.InterfaceMasquerade{}},
			}},
			annotations, false
	}

	var (
		networks   []kubevirtv1.Network
		interfaces []kubevirtv1.Interface
		staticIPs  []string
		hasBridge  bool
		needsDNS   bool
	)

	for idx, nic := range in.Networks {
		if nic.VLAN != "" {
			// Underlay: a multus attachment bridged into the guest.
			name := nic.VLAN
			if idx != 0 {
				name = fmt.Sprintf("%s-%d", nic.VLAN, idx)
			}
			multus := &kubevirtv1.MultusNetwork{
				NetworkName: fmt.Sprintf("%s/%s", in.VM.Namespace, nic.VLAN),
			}
			if idx == 0 {
				multus.Default = true
			}
			networks = append(networks, kubevirtv1.Network{
				Name:          name,
				NetworkSource: kubevirtv1.NetworkSource{Multus: multus},
			})
			interfaces = append(interfaces, kubevirtv1.Interface{
				Name:                   name,
				InterfaceBindingMethod: kubevirtv1.InterfaceBindingMethod{Bridge: &kubevirtv1.InterfaceBridge{}},
			})
			hasBridge = true
		} else {
			// VPC overlay: the pod network, steered to the right logical switch
			// by the annotation below. Only the primary can be one of these,
			// which admission enforces — a secondary would need a per-subnet
			// attachment definition wrapping the OVN CNI.
			binding := kubevirtv1.InterfaceBindingMethod{Bridge: &kubevirtv1.InterfaceBridge{}}
			if in.VM.Spec.NetworkBinding == "masquerade" {
				binding = kubevirtv1.InterfaceBindingMethod{Masquerade: &kubevirtv1.InterfaceMasquerade{}}
			} else {
				hasBridge = true
			}
			networks = append(networks, *kubevirtv1.DefaultPodNetwork())
			interfaces = append(interfaces, kubevirtv1.Interface{
				Name:                   "default",
				InterfaceBindingMethod: binding,
			})
			if nic.IsVPCOverlay {
				needsDNS = true
			}
		}
		if nic.StaticIP != "" {
			staticIPs = append(staticIPs, nic.StaticIP)
		}
	}

	// Only required when a bridge-bound interface is in play; pure pod-network
	// VMs migrate without it.
	if hasBridge {
		annotations[bridgeMigrationAnno] = "true"
	}
	// For a VPC overlay this attaches the pod to the right logical switch; for
	// a VLAN-backed primary it scopes DHCP and IPAM. Correct for both.
	annotations[logicalSwitchAnnotation] = in.Networks[0].Subnet
	if len(staticIPs) > 0 {
		annotations[ipAddressAnnotation] = strings.Join(staticIPs, ",")
	}

	return networks, interfaces, annotations, needsDNS
}

func ptr[T any](v T) *T { return &v }
