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

// TenantStorage is the allowance for workloads inside the tenant.
type TenantStorage struct {
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=10000
	// +kubebuilder:default=100
	// +optional
	AllowanceGi int32 `json:"allowanceGi,omitempty"`

	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=200
	// +kubebuilder:default=20
	// +optional
	PVCCount int32 `json:"pvcCount,omitempty"`
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

	// Storage is what the tenant's own workloads may provision inside its
	// namespace, through the CSI driver that turns a PVC in the tenant cluster
	// into a PVC on the host.
	//
	// Separate from the workers' disks, and added to them. They used to be two
	// ResourceQuota objects in one namespace, which Kubernetes applies
	// independently — so the effective cap was the smaller of the two — while
	// the folder ceiling, which sums every quota it finds, counted the tenant's
	// storage twice. Measured on this stand: one tenant with both objects
	// counted 220Gi against its folder while reserving 120Gi, and the tenant
	// beside it had only one object, so the double-count was not even
	// consistent.
	// +optional
	Storage TenantStorage `json:"storage,omitempty"`

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

	// ConditionNamespaceReady is false while the namespace, its single quota
	// and its LimitRange are not all in place.
	//
	// The LimitRange is not decoration. A quota on requests makes requests
	// mandatory, and Kamaji's control-plane containers declare none: without
	// defaults supplied, every tenant created in a folder had no control plane
	// at all, and the page said Provisioning forever.
	ConditionNamespaceReady = "NamespaceReady"

	// ConditionGoldenReady is the shared Talos image this tenant's workers are
	// cloned from — one per release, not one per tenant.
	//
	// False is not a refusal: CDI's clone waits for its source, so a golden
	// still importing delays the first worker's disk rather than failing the
	// tenant. It is here because "the tenant is stuck" and "a 20Gi image is
	// still downloading" look identical from the outside otherwise.
	ConditionGoldenReady = "GoldenReady"

	// ConditionAddressAssigned is the tenant's own address — the one its API
	// server, konnectivity, trustd and NTP all answer on.
	//
	// Its own, and not a shared one, because Talos derives trustd's address
	// from the control-plane endpoint and dials :50001 there. That port cannot
	// be moved, so two tenants cannot share a listener for it.
	ConditionAddressAssigned = "AddressAssigned"
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

	// ControlPlaneVIP is the address MetalLB gave this tenant. Published
	// because everything else is derived from it — the certificate's IP SAN,
	// the worker's endpoint, where its clock comes from — and a value that
	// several objects are built from should be visible in one place.
	// +optional
	ControlPlaneVIP string `json:"controlPlaneVIP,omitempty"`

	// TalosRelease is the release resolved from the catalogue, published
	// because "the default" is otherwise invisible and it decides which golden
	// image the workers clone.
	// +optional
	TalosRelease string `json:"talosRelease,omitempty"`

	// RedundantQuotas are other ResourceQuota objects in this namespace that
	// also cap storage.
	//
	// Reported rather than deleted: another writer put them there, and the
	// folder ceiling sums every quota it finds, so each one is counted again
	// against the folder. Named so somebody can decide.
	// +optional
	RedundantQuotas []string `json:"redundantQuotas,omitempty"`

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
