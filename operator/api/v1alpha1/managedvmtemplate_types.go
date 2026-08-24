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

// TemplateComputeSpec is the default size a template hands out.
type TemplateComputeSpec struct {
	// +kubebuilder:validation:Minimum=1
	Cores int32 `json:"cores"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=1
	// +optional
	Sockets int32 `json:"sockets,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=1
	// +optional
	Threads int32 `json:"threads,omitempty"`
	// +kubebuilder:validation:Pattern=`^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P|k|m)?$`
	Memory string `json:"memory"`
}

// TemplateRootDiskSpec is the default disk size.
//
// There is no storage class here, deliberately. Templates live on a cheap
// erasure-coded class and VM disks belong on the replicated one; inheriting the
// template's class sent every write a VM made to erasure coding.
type TemplateRootDiskSpec struct {
	// +kubebuilder:validation:Pattern=`^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P|k|m)?$`
	Size string `json:"size"`
}

// ManagedVMTemplateSpec is a named set of defaults for creating VMs.
type ManagedVMTemplateSpec struct {
	// DisplayName is what the picker shows.
	// +kubebuilder:validation:MaxLength=128
	// +optional
	DisplayName string `json:"displayName,omitempty"`

	// Description is free text shown next to it.
	// +kubebuilder:validation:MaxLength=512
	// +optional
	Description string `json:"description,omitempty"`

	// ImageRef names the ManagedImage VMs from this template are cloned from.
	//
	// A reference to our own resource, not to the generated name of a
	// DataVolume. The old store kept the literal generated name, which meant a
	// template could not be written declaratively at all: the name did not
	// exist until the image had been created and read back.
	ImageRef ImageRef `json:"imageRef"`

	// Compute defaults.
	Compute TemplateComputeSpec `json:"compute"`

	// RootDisk defaults.
	RootDisk TemplateRootDiskSpec `json:"rootDisk"`

	// CloudInit is the base user-data VMs merge their own into.
	// +optional
	CloudInit *CloudInitSpec `json:"cloudInit,omitempty"`

	// Console devices for VMs made from this template.
	// +optional
	Console *ConsoleSpec `json:"console,omitempty"`

	// OSType labels the guest operating system family.
	// +optional
	OSType string `json:"osType,omitempty"`

	// Category groups templates in the picker.
	// +kubebuilder:default=linux
	// +optional
	Category string `json:"category,omitempty"`
}

// ConditionImageFound reports whether the referenced image exists.
//
// Deliberately not whether it is *ready*: that is the image's own status, and
// mirroring it here would make two objects answer one question, which is how
// they end up disagreeing. A VM created from this template reports the import
// it is actually waiting on.
const ConditionImageFound = "ImageFound"

// ManagedVMTemplateStatus is what the picker and the VM controller read back.
type ManagedVMTemplateStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// ImageNamespace is where the referenced image was resolved, so callers do
	// not have to reimplement "empty means my own namespace".
	// +optional
	ImageNamespace string `json:"imageNamespace,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=mvmt
// +kubebuilder:printcolumn:name="Image",type=string,JSONPath=`.spec.imageRef.name`
// +kubebuilder:printcolumn:name="Cores",type=integer,JSONPath=`.spec.compute.cores`
// +kubebuilder:printcolumn:name="Memory",type=string,JSONPath=`.spec.compute.memory`
// +kubebuilder:printcolumn:name="Disk",type=string,JSONPath=`.spec.rootDisk.size`
// +kubebuilder:printcolumn:name="ImageFound",type=string,JSONPath=`.status.conditions[?(@.type=="ImageFound")].status`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ManagedVMTemplate is a named set of defaults for creating VMs.
//
// It replaces a single cluster-wide ConfigMap holding every template as a JSON
// blob under a user-chosen key — where a name collision answered 409 naming a
// template the user could not see, and two concurrent writes lost one of each
// other because the store was rewritten whole with no version check.
type ManagedVMTemplate struct {
	metav1.TypeMeta `json:",inline"`
	// metadata.name is what a VM's templateRef names, and the API server keeps
	// it unique per namespace.
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec ManagedVMTemplateSpec `json:"spec"`

	// +optional
	Status ManagedVMTemplateStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ManagedVMTemplateList contains a list of ManagedVMTemplate.
type ManagedVMTemplateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedVMTemplate `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion, &ManagedVMTemplate{}, &ManagedVMTemplateList{})
		return nil
	})
}
