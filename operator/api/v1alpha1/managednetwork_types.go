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

// NetworkStaticRoute is one route on the VPC router.
type NetworkStaticRoute struct {
	// +kubebuilder:validation:MinLength=1
	CIDR string `json:"cidr"`
	// +kubebuilder:validation:MinLength=1
	NextHopIP string `json:"nextHopIP"`
	// +kubebuilder:default=policyDst
	// +optional
	Policy string `json:"policy,omitempty"`
}

// ExternalPlane is how this network reaches anything outside itself.
type ExternalPlane struct {
	// Attachments are the subnets kube-ovn gives this VPC a router port on.
	//
	// Both halves of the attachment are required and each alone does nothing:
	// the master switch that makes kube-ovn read this array, and the array. A
	// VPC with the flag and an empty array got no external port; a VPC with the
	// array and no flag got no ports at all. Both were measured, a day apart,
	// and the flag looked sufficient only because the code that set it always
	// set the array too. This controller sets the flag whenever the array is
	// non-empty, so the pair cannot be written half-way.
	// +optional
	Attachments []string `json:"attachments,omitempty"`

	// EgressSubnet is the attachment the default route leaves through. Its
	// gateway address is read from the Subnet itself rather than configured:
	// the same number in two places is the same number until one of them
	// changes.
	//
	// Empty means no default route, and therefore a VPC that is attached but
	// does not leave. That is a legitimate shape — the control-plane transit
	// plane is exactly that — so it is not an error.
	// +optional
	EgressSubnet string `json:"egressSubnet,omitempty"`
}

// ManagedNetworkSpec is a tenant network: one kube-ovn VPC and its default
// subnet, described together because neither is useful alone.
type ManagedNetworkSpec struct {
	// CIDR of the default subnet.
	// +kubebuilder:validation:MinLength=1
	CIDR string `json:"cidr"`

	// Gateway inside CIDR. Empty means the first host address.
	// +optional
	Gateway string `json:"gateway,omitempty"`

	// Namespaces bound to this VPC. A namespace listed here and carrying no
	// explicit logical-switch annotation lands on the default subnet.
	// +optional
	Namespaces []string `json:"namespaces,omitempty"`

	// +optional
	Tenant string `json:"tenant,omitempty"`
	// +optional
	Folder string `json:"folder,omitempty"`
	// +optional
	Environment string `json:"environment,omitempty"`

	// Role is declared, never inferred.
	//
	// The isolation census used to read the role off the shape of the object
	// and counted the egress gateway's own VPC as a tenant, which handed every
	// tenant a drop on the very address it egresses through. `infrastructure`
	// means this network serves the others.
	// +optional
	Role string `json:"role,omitempty"`

	// Isolated states whether this network is closed to other tenant networks.
	//
	// It records the decision; it does not write the rules. The ACL composer
	// owns `Subnet.spec.acls`, and two writers of one list is the failure this
	// operator exists to remove. What this flag does here is stamp the opt-out
	// annotation the isolation reconciler reads — written only when the answer
	// is "no", so that its absence means "no choice recorded" and the default
	// is to isolate. The old default ran the other way, and silence read as
	// consent to stay open.
	// +kubebuilder:default=true
	// +optional
	Isolated *bool `json:"isolated,omitempty"`

	// SharedCIDRs are prefixes this network may still reach while isolated.
	// +optional
	SharedCIDRs []string `json:"sharedCIDRs,omitempty"`

	// +optional
	StaticRoutes []NetworkStaticRoute `json:"staticRoutes,omitempty"`

	// NATGateway turns on kube-ovn's per-VPC SNAT.
	//
	// Off unless deliberately asked for. kube-ovn keeps one SNAT per logical
	// IP, and on a VPC that also carries a control-plane transit leg that slot
	// belongs to the transit path. Taking it gives the tenant internet and no
	// control plane, with every object reporting healthy — which is how it went
	// unnoticed.
	// +kubebuilder:default=false
	// +optional
	NATGateway bool `json:"natGateway,omitempty"`

	// DNSServer is handed to workloads over DHCP.
	//
	// Empty means the subnet's DHCP options carry no `dns_server`, and
	// workloads get no resolver from the network. That is worth being explicit
	// about rather than defaulting to something plausible: a wrong resolver
	// address looks exactly like a working one until something tries to
	// resolve.
	// +optional
	DNSServer string `json:"dnsServer,omitempty"`

	// +optional
	ExternalPlane *ExternalPlane `json:"externalPlane,omitempty"`
}

// Condition types published by the network controller.
const (
	// ConditionNetworkReady is false while the VPC or its default subnet is
	// missing, refused, or not yet reported ready by kube-ovn.
	ConditionNetworkReady = "Ready"

	// ConditionAttached is false when the external plane was asked for and the
	// VPC does not have it.
	//
	// Separate from Ready because it fails separately and silently: a VPC with
	// no external port is a perfectly healthy VPC that nothing can leave.
	ConditionAttached = "Attached"
)

// ManagedNetworkStatus is what the network actually is.
type ManagedNetworkStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// +optional
	SubnetName string `json:"subnetName,omitempty"`
	// +optional
	Gateway string `json:"gateway,omitempty"`

	// DefaultRouteVia is the next hop actually written, read from the egress
	// subnet rather than configured. Published because the announcement
	// generator decides what to advertise from this same fact — a network is
	// announced when its default route leads into the external subnet — so the
	// datapath and the announcement cannot drift apart.
	// +optional
	DefaultRouteVia string `json:"defaultRouteVia,omitempty"`

	// +optional
	Attachments []string `json:"attachments,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=mnet
// +kubebuilder:printcolumn:name="CIDR",type=string,JSONPath=`.spec.cidr`
// +kubebuilder:printcolumn:name="Tenant",type=string,JSONPath=`.spec.tenant`
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
// +kubebuilder:printcolumn:name="Attached",type=string,JSONPath=`.status.conditions[?(@.type=="Attached")].status`
// +kubebuilder:printcolumn:name="Via",type=string,JSONPath=`.status.defaultRouteVia`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ManagedNetwork is a tenant network.
type ManagedNetwork struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec ManagedNetworkSpec `json:"spec"`

	// +optional
	Status ManagedNetworkStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ManagedNetworkList contains a list of ManagedNetwork.
type ManagedNetworkList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedNetwork `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion, &ManagedNetwork{}, &ManagedNetworkList{})
		return nil
	})
}
