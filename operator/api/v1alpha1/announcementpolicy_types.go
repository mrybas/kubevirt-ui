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

// AnnouncementPolicySpec is how tenant networks are advertised to the border.
//
// It is configuration read from the cluster rather than from the process's
// environment, which is the point: these values are properties of an
// installation's addressing plan, and one of them being unset used to mean the
// whole feature quietly did nothing on an entire stand.
type AnnouncementPolicySpec struct {
	// BorderPeer is the address BGP is spoken to.
	//
	// Deliberately separate from the gateway a VPC's default route points at:
	// they are different addresses of the same box, and conflating them
	// produces networks whose default route points at a management address the
	// external plane cannot reach.
	// +kubebuilder:validation:MinLength=1
	BorderPeer string `json:"borderPeer"`

	// LocalASN is this cluster's autonomous system number.
	// +kubebuilder:validation:Minimum=1
	LocalASN int32 `json:"localASN"`

	// PeerASN is the border's.
	// +kubebuilder:validation:Minimum=1
	PeerASN int32 `json:"peerASN"`

	// ExternalSubnet is the kube-ovn subnet the router legs sit in. The next
	// hops live there, and a VPC counts as routed when its default route points
	// into it.
	// +kubebuilder:default=external
	// +optional
	ExternalSubnet string `json:"externalSubnet,omitempty"`

	// TargetNamespace is where the FRRConfiguration is written — wherever
	// frr-k8s watches.
	// +kubebuilder:default=metallb-system
	// +optional
	TargetNamespace string `json:"targetNamespace,omitempty"`

	// Replicas is how many nodes carry the announcement.
	//
	// Redundancy of the announcement, not of the path: every node advertises
	// the same prefix with the same next hop, so there is no traffic to split.
	// One node dying must not take a tenant's return path with it; that is all
	// this is for.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=2
	// +optional
	Replicas int32 `json:"replicas,omitempty"`

	// Nodes pins the announcement to named nodes, overriding the automatic
	// choice.
	//
	// The automatic choice is Ready workers, sorted. Control-plane nodes are
	// excluded because the border peers with workers: a plain sort over every
	// node put the control plane first, and every prefix silently vanished from
	// the border while this object looked perfect.
	// +optional
	Nodes []string `json:"nodes,omitempty"`
}

// AnnouncedPrefix is one advertised network and where the border should send it.
type AnnouncedPrefix struct {
	VPC     string `json:"vpc"`
	CIDR    string `json:"cidr"`
	NextHop string `json:"nextHop"`
}

// NodeReloadFailure is FRR refusing what was generated, in its own words.
type NodeReloadFailure struct {
	Node    string `json:"node"`
	Message string `json:"message"`
}

// Condition types published by the announcement controller.
const (
	// ConditionAccepted is false when FRR rejected the generated configuration
	// on any node.
	ConditionAccepted = "Accepted"
)

// AnnouncementPolicyStatus is what is actually being advertised, and whether
// FRR took it.
type AnnouncementPolicyStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Announced is what the generated configuration advertises.
	// +optional
	Announced []AnnouncedPrefix `json:"announced,omitempty"`

	// Nodes are the nodes carrying it.
	// +optional
	Nodes []string `json:"nodes,omitempty"`

	// ReloadFailures are nodes where FRR refused the configuration.
	//
	// A rejected reload is fail-safe in the wrong direction: FRR keeps the
	// previous configuration, so live announcements survive and a newly
	// attached network is silently not added — exactly the "attached but not
	// announced" state this design exists to avoid. Nothing else reports it.
	// +optional
	ReloadFailures []NodeReloadFailure `json:"reloadFailures,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=annpol
// +kubebuilder:printcolumn:name="Peer",type=string,JSONPath=`.spec.borderPeer`
// +kubebuilder:printcolumn:name="Prefixes",type=integer,JSONPath=`.status.announced[*]`,priority=1
// +kubebuilder:printcolumn:name="Accepted",type=string,JSONPath=`.status.conditions[?(@.type=="Accepted")].status`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
// +kubebuilder:validation:XValidation:rule="self.metadata.name == 'default'",message="there is one announcement policy per cluster and it is called default"

// AnnouncementPolicy is how this cluster advertises its tenant networks.
type AnnouncementPolicy struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec AnnouncementPolicySpec `json:"spec"`

	// +optional
	Status AnnouncementPolicyStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AnnouncementPolicyList contains a list of AnnouncementPolicy.
type AnnouncementPolicyList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AnnouncementPolicy `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion, &AnnouncementPolicy{}, &AnnouncementPolicyList{})
		return nil
	})
}
