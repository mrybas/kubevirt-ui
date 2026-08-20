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

// TenantWorkers is the shape of the worker pool.
type TenantWorkers struct {
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=20
	// +kubebuilder:default=2
	// +optional
	Count int32 `json:"count,omitempty"`

	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=32
	// +kubebuilder:default=2
	// +optional
	VCPU int32 `json:"vcpu,omitempty"`

	// +kubebuilder:default="2Gi"
	// +optional
	Memory string `json:"memory,omitempty"`

	// +kubebuilder:default="20Gi"
	// +optional
	Disk string `json:"disk,omitempty"`

	// OS decides how a worker is bootstrapped, and they are different
	// mechanisms rather than different images. `cloud-init` boots a CAPK
	// container-disk through a KubeadmConfigTemplate; `talos` swaps the
	// bootstrap provider and brings its own per-tenant objects — a CSR signer
	// with its own PKI, stable machine secrets, a bootstrap token. A Talos node
	// cannot be bootstrapped the cloud-init way at all: it asks a trustd signer
	// for a certificate instead.
	// +kubebuilder:validation:Enum=cloud-init;talos
	// +kubebuilder:default=cloud-init
	// +optional
	OS string `json:"os,omitempty"`

	// TalosVersion picks the release to build Talos workers from. Empty means
	// the catalogue's default. Validated against the same compatibility the
	// wizard renders its list from, so the two cannot offer and refuse
	// different things.
	// +optional
	TalosVersion string `json:"talosVersion,omitempty"`
}

// ManagedTenantSpec is a tenant Kubernetes cluster.
type ManagedTenantSpec struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	DisplayName string `json:"displayName"`

	// Folder and Environment are what the tenant is scoped to, and they are
	// required: a tenant outside them takes no part in folder authorisation and
	// spends nobody's quota.
	// +kubebuilder:validation:MinLength=1
	Folder string `json:"folder"`
	// +kubebuilder:validation:MinLength=1
	Environment string `json:"environment"`

	// +kubebuilder:default="v1.30.1"
	// +optional
	KubernetesVersion string `json:"kubernetesVersion,omitempty"`

	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=3
	// +kubebuilder:default=2
	// +optional
	ControlPlaneReplicas int32 `json:"controlPlaneReplicas,omitempty"`

	// +optional
	Workers TenantWorkers `json:"workers,omitempty"`

	// Network is the VPC the tenant's machines live in. Empty means the
	// cluster's default overlay.
	// +optional
	Network string `json:"network,omitempty"`

	// +kubebuilder:default="10.244.0.0/16"
	// +optional
	PodCIDR string `json:"podCIDR,omitempty"`

	// ServiceCIDR is deliberately not the host cluster's own 10.96.0.0/12.
	//
	// A tenant sharing it makes its kube-proxy hijack the host's DNS address
	// for the tenant's own CoreDNS, and its nodes lose name resolution the
	// moment kube-proxy starts — after they have already joined. 10.112.0.0/12
	// is the adjacent block, disjoint from it, and the host's actual ranges are
	// still checked because a cluster may of course use 10.112 itself.
	// +kubebuilder:default="10.112.0.0/12"
	// +optional
	ServiceCIDR string `json:"serviceCIDR,omitempty"`
}

// Condition types published by the tenant controller.
const (
	// ConditionTenantAccepted is false when the request cannot be built as
	// asked — a Talos release that does not take the requested Kubernetes, a
	// folder that does not exist.
	//
	// The distinction that matters: a thing that will never become true is a
	// refusal, and a thing that is merely not true yet is a condition. An
	// incompatible version pair is the first; a network that has not been
	// created is the second, and the tenant waits for it.
	ConditionTenantAccepted = "Accepted"

	// ConditionQuotaReserved is the folder ceiling's answer, taken before
	// anything exists — the one place a ceiling can legally refuse.
	ConditionQuotaReserved = "QuotaReserved"
)

// TenantReservation is what this tenant asks of its folder.
type TenantReservation struct {
	// +optional
	CPU string `json:"cpu,omitempty"`
	// +optional
	Memory string `json:"memory,omitempty"`
	// +optional
	Storage string `json:"storage,omitempty"`
}

// ManagedTenantStatus is what the tenant actually is.
type ManagedTenantStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Namespace the tenant's objects live in.
	// +optional
	Namespace string `json:"namespace,omitempty"`

	// TalosRelease is the release resolved from the catalogue, published
	// because "the default" is otherwise invisible and it decides which golden
	// image the workers clone.
	// +optional
	TalosRelease string `json:"talosRelease,omitempty"`

	// Reservation is what was sized from the request, before any of it exists.
	// +optional
	Reservation *TenantReservation `json:"reservation,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=mten
// +kubebuilder:printcolumn:name="Folder",type=string,JSONPath=`.spec.folder`
// +kubebuilder:printcolumn:name="Env",type=string,JSONPath=`.spec.environment`
// +kubebuilder:printcolumn:name="K8s",type=string,JSONPath=`.spec.kubernetesVersion`
// +kubebuilder:printcolumn:name="Workers",type=integer,JSONPath=`.spec.workers.count`
// +kubebuilder:printcolumn:name="Accepted",type=string,JSONPath=`.status.conditions[?(@.type=="Accepted")].status`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ManagedTenant is a tenant Kubernetes cluster.
type ManagedTenant struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec ManagedTenantSpec `json:"spec"`

	// +optional
	Status ManagedTenantStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ManagedTenantList contains a list of ManagedTenant.
type ManagedTenantList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedTenant `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion, &ManagedTenant{}, &ManagedTenantList{})
		return nil
	})
}
