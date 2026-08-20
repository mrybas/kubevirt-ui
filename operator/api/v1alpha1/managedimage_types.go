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

// HTTPSource imports the image over HTTP(S).
type HTTPSource struct {
	// URL of the disk image to download.
	// +kubebuilder:validation:MinLength=1
	URL string `json:"url"`
}

// RegistrySource imports the image from a container registry.
type RegistrySource struct {
	// URL of the container image, e.g. docker://quay.io/org/image:tag.
	// +kubebuilder:validation:MinLength=1
	URL string `json:"url"`
}

// PVCSource clones an existing PersistentVolumeClaim.
type PVCSource struct {
	// Name of the source PersistentVolumeClaim.
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// Namespace of the source claim. Empty means this image's own namespace.
	// A foreign namespace additionally requires the CDI clone gate
	// (create on datavolumes/source in the source namespace), which the
	// controller reports rather than silently hanging on.
	// +optional
	Namespace string `json:"namespace,omitempty"`
}

// BlankSource allocates an empty disk.
type BlankSource struct{}

// ManagedImageSource is where the image content comes from. Exactly one member
// must be set — the same four sources the UI has always offered.
// +kubebuilder:validation:XValidation:rule="[has(self.http), has(self.registry), has(self.pvc), has(self.blank)].exists_one(x, x)",message="exactly one of http, registry, pvc or blank must be set"
type ManagedImageSource struct {
	// +optional
	HTTP *HTTPSource `json:"http,omitempty"`
	// +optional
	Registry *RegistrySource `json:"registry,omitempty"`
	// +optional
	PVC *PVCSource `json:"pvc,omitempty"`
	// +optional
	Blank *BlankSource `json:"blank,omitempty"`
}

// ManagedImageSpec describes a disk image the platform manages.
//
// Fields that cannot be changed after the import are marked immutable rather
// than accepted and ignored: CDI will not re-import a DataVolume because its
// source changed, and an edit that looks applied but does nothing is the defect
// this API exists to remove.
type ManagedImageSpec struct {
	// DisplayName is what humans see. Cosmetic only: it never takes part in the
	// name of anything the controller creates.
	// +kubebuilder:validation:MaxLength=128
	// +optional
	DisplayName string `json:"displayName,omitempty"`

	// Description is free text shown in the UI.
	// +kubebuilder:validation:MaxLength=512
	// +optional
	Description string `json:"description,omitempty"`

	// Source of the image content.
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="source is immutable: create a new ManagedImage instead — CDI does not re-import an existing disk"
	Source ManagedImageSource `json:"source"`

	// Size of the disk to allocate, e.g. 20Gi.
	// +kubebuilder:validation:Pattern=`^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P|k|m)?$`
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="size is immutable: resize the underlying claim deliberately, not by editing the image"
	Size string `json:"size"`

	// StorageClass to allocate from. Empty means the cluster default.
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="storageClass is immutable: the disk already exists on a class"
	// +kubebuilder:default=""
	// +optional
	StorageClass string `json:"storageClass,omitempty"`

	// Scope decides who may reference this image.
	//
	//   environment — this namespace only (the default, and the private case)
	//   project     — every environment of the same project
	//   folder      — the folder and its descendants
	//
	// All three values have always been understood by the image lister; only the
	// first two were ever writable, so folder-scoped images could until now be
	// produced only by labelling a DataVolume by hand.
	// +kubebuilder:validation:Enum=environment;project;folder
	// +kubebuilder:default=environment
	// +optional
	Scope string `json:"scope,omitempty"`

	// DiskType separates bootable images from plain data disks.
	// +kubebuilder:validation:Enum=image;data
	// +kubebuilder:default=image
	// +optional
	DiskType string `json:"diskType,omitempty"`

	// Persistent marks a disk that is attached rather than cloned.
	// +kubebuilder:default=false
	// +optional
	Persistent bool `json:"persistent,omitempty"`

	// OSType labels the guest operating system family, e.g. linux.
	// +optional
	OSType string `json:"osType,omitempty"`

	// OSVersion labels the guest operating system version, e.g. 24.04.
	// +optional
	OSVersion string `json:"osVersion,omitempty"`
}

// Image phases. Deliberately fewer than CDI's: the product only distinguishes
// "not started", "working on it", "usable" and "broken".
const (
	// ImagePhasePending means the underlying disk has not been created yet.
	ImagePhasePending = "Pending"
	// ImagePhaseImporting means CDI is importing or cloning.
	ImagePhaseImporting = "Importing"
	// ImagePhaseReady means the disk is complete and can be cloned from.
	ImagePhaseReady = "Ready"
	// ImagePhaseFailed means the import failed and will not finish on its own.
	ImagePhaseFailed = "Failed"
)

// Condition types published by the ManagedImage controller.
const (
	// ConditionReady is true only when the disk is importable-from.
	ConditionReady = "Ready"
	// ConditionDeleting is true while deletion is being held back, and its
	// message names what is holding it.
	ConditionDeleting = "Deleting"
)

// ManagedImageStatus is what the UI and Terraform read back.
type ManagedImageStatus struct {
	// ObservedGeneration is the spec generation this status was computed from.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Phase is the coarse state: Pending, Importing, Ready or Failed.
	// +kubebuilder:validation:Enum=Pending;Importing;Ready;Failed
	// +optional
	Phase string `json:"phase,omitempty"`

	// Progress is CDI's import progress, passed through verbatim, e.g. "42%".
	// +optional
	Progress string `json:"progress,omitempty"`

	// DataVolumeName is the DataVolume backing this image. Read it from here —
	// never reconstruct it from the image name.
	// +optional
	DataVolumeName string `json:"dataVolumeName,omitempty"`

	// DataSourceName is the CDI DataSource published for this image, so that
	// KubeVirt-native consumers can reference it without knowing about us.
	// +optional
	DataSourceName string `json:"dataSourceName,omitempty"`

	// UsedBy lists the workloads currently cloning from or attached to this
	// image, as namespace/name. Deletion is refused while it is not empty:
	// removing a clone source mid-clone leaves an orphaned disk behind.
	// +optional
	UsedBy []string `json:"usedBy,omitempty"`

	// Conditions carry the reasons. A blocked image always names its blocker.
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=mimg
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Progress",type=string,JSONPath=`.status.progress`
// +kubebuilder:printcolumn:name="Size",type=string,JSONPath=`.spec.size`
// +kubebuilder:printcolumn:name="Scope",type=string,JSONPath=`.spec.scope`
// +kubebuilder:printcolumn:name="DataVolume",type=string,JSONPath=`.status.dataVolumeName`,priority=1
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ManagedImage is a disk image the platform owns: one object a user or a
// Terraform module can name, instead of a generated DataVolume name that had to
// be read back before anything could reference it.
type ManagedImage struct {
	metav1.TypeMeta `json:",inline"`

	// metadata.name is the contract. Every reference to this image — from a
	// template, from a VM, from a module — uses it, and the API server keeps it
	// unique per namespace for free.
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec ManagedImageSpec `json:"spec"`

	// +optional
	Status ManagedImageStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ManagedImageList contains a list of ManagedImage.
type ManagedImageList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedImage `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion, &ManagedImage{}, &ManagedImageList{})
		return nil
	})
}
