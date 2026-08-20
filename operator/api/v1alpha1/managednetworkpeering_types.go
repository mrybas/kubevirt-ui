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

// ManagedNetworkPeeringSpec is a direct link between two networks.
//
// One object, both ends. A peering is not a thing either router owns — it is
// half an entry in each of two foreign specs plus a point-to-point link between
// them — and describing it as two objects would mean two things that have to
// agree, which is the shape that produced peerings written on one side only.
type ManagedNetworkPeeringSpec struct {
	// Networks are the two VPCs to connect. Unordered: a peering is symmetric,
	// and naming one of them "local" is a property of who asked, not of the
	// link.
	// +kubebuilder:validation:MinItems=2
	// +kubebuilder:validation:MaxItems=2
	// +kubebuilder:validation:XValidation:rule="self[0] != self[1]",message="a network cannot peer with itself"
	Networks []string `json:"networks"`

	// LinkCIDR pins the point-to-point subnet. Empty means the lowest free one
	// is chosen, which is what should normally happen: the address matters to
	// nobody except the two routers holding it.
	// +optional
	LinkCIDR string `json:"linkCIDR,omitempty"`
}

// PeeringLeg is one end of the link, and whether it has been written.
//
// Recorded in status rather than kept in a variable, and that is the whole
// point of it existing. A peering written on one side only is a black hole —
// the other router has no way back — so the second leg failing has to undo the
// first. Held in memory, that undo is lost the moment the process restarts, and
// what is left behind is exactly the half-peering nobody wanted.
type PeeringLeg struct {
	Network string `json:"network"`
	// +optional
	ConnectIP string `json:"connectIP,omitempty"`
	// +optional
	Applied bool `json:"applied,omitempty"`
}

// Condition types published by the peering controller.
const (
	// ConditionEstablished is true only when both ends are written. There is no
	// partial success here worth reporting as success.
	ConditionEstablished = "Established"
)

// ManagedNetworkPeeringStatus is what was actually written.
type ManagedNetworkPeeringStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// LinkCIDR is the point-to-point subnet in use.
	// +optional
	LinkCIDR string `json:"linkCIDR,omitempty"`

	// Legs is both ends and their state, written before each end is attempted
	// so a crash leaves a record of what to undo.
	// +optional
	Legs []PeeringLeg `json:"legs,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=mnpeer
// +kubebuilder:printcolumn:name="Networks",type=string,JSONPath=`.spec.networks`
// +kubebuilder:printcolumn:name="Link",type=string,JSONPath=`.status.linkCIDR`
// +kubebuilder:printcolumn:name="Established",type=string,JSONPath=`.status.conditions[?(@.type=="Established")].status`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ManagedNetworkPeering connects two networks directly.
//
// Without it a tenant reaching a shared network goes out through its egress
// gateway, across the upstream router, and back in through the other gateway:
// four extra hops, with every shared-service flow crossing the lab router.
//
// Deleting this object removes both ends. That is safe in a way deleting a
// ManagedNetwork is not — a peering has no addresses of its own and nothing
// runs on it.
type ManagedNetworkPeering struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec ManagedNetworkPeeringSpec `json:"spec"`

	// +optional
	Status ManagedNetworkPeeringStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ManagedNetworkPeeringList contains a list of ManagedNetworkPeering.
type ManagedNetworkPeeringList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedNetworkPeering `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion,
			&ManagedNetworkPeering{}, &ManagedNetworkPeeringList{})
		return nil
	})
}
