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

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// ImageRef points at a ManagedImage to clone the root disk from.
type ImageRef struct {
	// Name of the ManagedImage.
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// Namespace of the ManagedImage. Empty means this VM's own namespace.
	//
	// A foreign namespace is allowed only when the image's scope says so and
	// both namespaces belong to the same project or folder. That check is not a
	// formality: measured on the cluster, KubeVirt's admission does not stop a
	// cross-namespace clone, and the clone itself succeeds as long as a Role
	// exists — so this scope rule, not RBAC, is what keeps one team's disk out
	// of another team's VM.
	// +optional
	Namespace string `json:"namespace,omitempty"`
}

// TemplateRef points at a template carrying the defaults for this VM.
type TemplateRef struct {
	// Name of the template.
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// ComputeSpec is what the guest gets.
type ComputeSpec struct {
	// Cores visible to the guest.
	// +kubebuilder:validation:Minimum=1
	Cores int32 `json:"cores"`

	// Sockets, defaulting to one.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=1
	// +optional
	Sockets int32 `json:"sockets,omitempty"`

	// Threads per core, defaulting to one.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=1
	// +optional
	Threads int32 `json:"threads,omitempty"`

	// Memory for the guest, e.g. 4Gi.
	// +kubebuilder:validation:Pattern=`^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P|k|m)?$`
	Memory string `json:"memory"`
}

// RootDiskSpec is the VM's own copy of the image.
type RootDiskSpec struct {
	// Size of the root disk. Must be at least the size of the image it is
	// cloned from — CDI refuses a smaller target.
	// +kubebuilder:validation:Pattern=`^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P|k|m)?$`
	Size string `json:"size"`

	// StorageClass for the root disk. Empty means the cluster default.
	//
	// It is deliberately not inherited from the image: images sit on a cheap
	// erasure-coded class and VM disks belong on the replicated one, and
	// inheriting sent every write a VM made to erasure coding.
	// +kubebuilder:default=""
	// +optional
	StorageClass string `json:"storageClass,omitempty"`
}

// NetworkAttachment is one NIC.
type NetworkAttachment struct {
	// Subnet is the kube-ovn subnet to attach to.
	// +kubebuilder:validation:MinLength=1
	Subnet string `json:"subnet"`

	// StaticIP pins the address instead of taking one from IPAM.
	// +optional
	StaticIP string `json:"staticIP,omitempty"`
}

// DiskAttachment is an existing claim attached to the machine.
//
// The root disk is not one of these: it is created with the machine and is
// described by rootDisk. These are disks that exist on their own and are
// plugged in — which is why they are matched by claim rather than rendered,
// and why the machine's own root entry is never touched by this list.
type DiskAttachment struct {
	// Claim is the DataVolume or PersistentVolumeClaim to attach, in this
	// namespace.
	// +kubebuilder:validation:MinLength=1
	Claim string `json:"claim"`

	// Bus the guest sees the disk on.
	// +kubebuilder:validation:Enum=virtio;scsi;sata
	// +kubebuilder:default=virtio
	// +optional
	Bus string `json:"bus,omitempty"`
}

// SSHSpec carries the keys to install in the guest.
//
// The keys are explicit. The UI injects the requesting user's profile keys when
// it translates a form into this object, which keeps "whose keys" a question
// about a person — something a controller reconciling a stored object has no
// way to answer, and today's code answers by silently installing none when the
// profile read fails.
type SSHSpec struct {
	// AuthorizedKeys to write into the guest.
	// +optional
	AuthorizedKeys []string `json:"authorizedKeys,omitempty"`
}

// SecretKeyRef names one key of one Secret.
type SecretKeyRef struct {
	// Name of the Secret, in this VM's namespace.
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// Key inside the Secret.
	// +kubebuilder:default=password
	// +optional
	Key string `json:"key,omitempty"`
}

// CloudInitSpec is the user-data merged into the guest's cloud-init.
type CloudInitSpec struct {
	// UserData is a #cloud-config document.
	// +optional
	UserData string `json:"userData,omitempty"`
}

// ConsoleSpec decides which consoles the guest exposes.
type ConsoleSpec struct {
	// VNC attaches a graphics device.
	// +kubebuilder:default=true
	// +optional
	VNC *bool `json:"vnc,omitempty"`
	// Serial attaches a serial console.
	// +kubebuilder:default=false
	// +optional
	Serial *bool `json:"serial,omitempty"`
}

// ManagedVMSpec is a virtual machine as the product describes one.
//
// It is deliberately not a KubeVirt VirtualMachine with extra fields: the
// point is that a caller says what they want and the controller renders the
// dataVolumeTemplates, the cloud-init, the overcommit arithmetic, the network
// annotations and the VPC resolver — the parts that were previously spelled out
// only inside one HTTP handler and therefore did not exist for anyone writing
// manifests directly.
// +kubebuilder:validation:XValidation:rule="has(self.templateRef) != has(self.imageRef)",message="set exactly one of templateRef or imageRef"
// +kubebuilder:validation:XValidation:rule="!has(self.imageRef) || (has(self.compute) && has(self.rootDisk))",message="compute and rootDisk are required when imageRef is used: there is no template to take defaults from"
type ManagedVMSpec struct {
	// DisplayName is the human-facing name.
	// +kubebuilder:validation:MaxLength=100
	// +optional
	DisplayName string `json:"displayName,omitempty"`

	// TemplateRef takes defaults from a template. Mutually exclusive with
	// imageRef.
	// +optional
	TemplateRef *TemplateRef `json:"templateRef,omitempty"`

	// ImageRef clones straight from an image, with no template in between.
	// Mutually exclusive with templateRef.
	// +optional
	ImageRef *ImageRef `json:"imageRef,omitempty"`

	// Compute overrides the template's defaults, and is required without one.
	// +optional
	Compute *ComputeSpec `json:"compute,omitempty"`

	// RootDisk overrides the template's default, and is required without one.
	// +optional
	RootDisk *RootDiskSpec `json:"rootDisk,omitempty"`

	// Disks attached to the machine, beyond its own root disk.
	//
	// Declarative, so that what is attached is visible in one place and
	// survives whatever happens to the process that attached it. A claim marked
	// persistent may be listed by exactly one machine — admission checks that,
	// because two machines writing one disk corrupts it and neither of them
	// finds out.
	// +optional
	// +listType=map
	// +listMapKey=claim
	Disks []DiskAttachment `json:"disks,omitempty"`

	// Networks in order. The first is the primary NIC.
	//
	// Only the primary may be a VPC subnet: a secondary VPC NIC would need a
	// per-subnet attachment definition wrapping the OVN CNI, which does not
	// exist here. Additional NICs must be VLAN-backed.
	// +optional
	Networks []NetworkAttachment `json:"networks,omitempty"`

	// NetworkBinding for a VPC-attached primary NIC.
	//
	// bridge lets the guest report its real overlay address; masquerade is the
	// legacy alternative and breaks multicast.
	// +kubebuilder:validation:Enum=bridge;masquerade
	// +kubebuilder:default=bridge
	// +optional
	NetworkBinding string `json:"networkBinding,omitempty"`

	// SSH keys to install in the guest.
	// +optional
	SSH *SSHSpec `json:"ssh,omitempty"`

	// InitialPasswordSecretRef names a Secret holding the first-boot password.
	//
	// A reference rather than the password itself: this object ends up in etcd
	// and, for anything managed as code, in a state file.
	// +optional
	InitialPasswordSecretRef *SecretKeyRef `json:"initialPasswordSecretRef,omitempty"`

	// CloudInit is merged with the template's user-data.
	// +optional
	CloudInit *CloudInitSpec `json:"cloudInit,omitempty"`

	// Console devices.
	// +optional
	Console *ConsoleSpec `json:"console,omitempty"`

	// Running asks for the VM to be on.
	//
	// The controller owns runStrategy on the rendered VirtualMachine, so a
	// `virtctl start/stop` against a managed VM is reverted — with an event, never
	// silently. Infrastructure-as-code should ignore changes to this field.
	// +kubebuilder:default=true
	// +optional
	Running bool `json:"running"`
}

// ManagedVM condition types.
const (
	// ConditionProvisioned is true once the VirtualMachine has been rendered.
	ConditionProvisioned = "Provisioned"
	// ConditionImageReady reports the state of the image this VM clones from.
	ConditionImageReady = "ImageReady"
)

// ManagedVMStatus is the derived state, kept deliberately small.
//
// The fast-moving facts of a running guest — address, node, guest-agent
// details, VMI phase — are not mirrored here. Copying them would mean an etcd
// write every time a guest breathes, multiplied by every VM on the cluster,
// to reproduce data the UI can already watch at the source.
type ManagedVMStatus struct {
	// ObservedGeneration is the spec generation this status was computed from.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// VirtualMachineName is the KubeVirt VirtualMachine backing this VM. It
	// equals metadata.name; it is published so that code reads it instead of
	// assuming the rule.
	// +optional
	VirtualMachineName string `json:"virtualMachineName,omitempty"`

	// RootDiskName is the DataVolume holding this VM's root disk.
	// +optional
	RootDiskName string `json:"rootDiskName,omitempty"`

	// OperationInProgress names the ManagedVMOperation currently acting on this
	// machine, if any.
	//
	// Derived, not told: the VM controller works it out by looking for
	// unfinished operations that target it, rather than having another
	// controller write into this status. Two writers of one object is the
	// defect this whole design is built to avoid, and it would be an odd place
	// to make an exception.
	// +optional
	OperationInProgress string `json:"operationInProgress,omitempty"`

	// AttachedDisks lists the claims this controller has plugged in.
	//
	// Kept so that removing a disk from the spec detaches exactly what was
	// attached from it, and nothing else. A disk plugged in by some other route
	// is left alone rather than reclaimed and then removed as unrecognised.
	// +optional
	AttachedDisks []string `json:"attachedDisks,omitempty"`

	// RootDiskEpoch counts how many times the root disk has been provisioned.
	// It appears in the disk's name so that a replacement cannot collide with a
	// predecessor that is still terminating.
	// +optional
	RootDiskEpoch int32 `json:"rootDiskEpoch,omitempty"`

	// Conditions carry the reasons.
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=mvm
// +kubebuilder:printcolumn:name="Running",type=boolean,JSONPath=`.spec.running`
// +kubebuilder:printcolumn:name="VM",type=string,JSONPath=`.status.virtualMachineName`
// +kubebuilder:printcolumn:name="Provisioned",type=string,JSONPath=`.status.conditions[?(@.type=="Provisioned")].status`
// +kubebuilder:printcolumn:name="Image",type=string,JSONPath=`.status.conditions[?(@.type=="ImageReady")].reason`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ManagedVM is a virtual machine the platform owns.
type ManagedVM struct {
	metav1.TypeMeta `json:",inline"`

	// metadata.name is the contract, and the KubeVirt VirtualMachine is given
	// the same name — a one-to-one child needs no separate identity.
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec ManagedVMSpec `json:"spec"`

	// +optional
	Status ManagedVMStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ManagedVMList contains a list of ManagedVM.
type ManagedVMList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedVM `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion, &ManagedVM{}, &ManagedVMList{})
		return nil
	})
}
