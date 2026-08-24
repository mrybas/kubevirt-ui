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

// ManagedUnderlaySpec describes the physical path out of the cluster that VPC
// egress gateways attach to: a NIC, a VLAN, and an external subnet reachable
// through a network-attachment-definition.
type ManagedUnderlaySpec struct {
	// Interface is the dedicated NIC for the provider network, e.g. `eth1`.
	//
	// It must not be the node's management interface. kube-ovn enslaves the NIC
	// into the OVS bridge and migrates its address there, which on Talos does
	// not hold: the node loses its address and stays NotReady until it is
	// rebooted.
	// +kubebuilder:validation:MinLength=1
	Interface string `json:"interface"`

	// ExternalCIDR is the segment on the other side of that NIC.
	// +kubebuilder:validation:MinLength=1
	ExternalCIDR string `json:"externalCIDR"`

	// ExternalGateway is the upstream address on that segment.
	// +kubebuilder:validation:MinLength=1
	ExternalGateway string `json:"externalGateway"`

	// VLANID is the tag, 0 for untagged.
	//
	// Tagged frames do not always survive an overlay underneath — measured on
	// an OpenNebula VXLAN lab, where two pods on different workers could not
	// ARP each other while untagged frames between the same NICs were fine.
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=4094
	// +kubebuilder:default=0
	// +optional
	VLANID int32 `json:"vlanID,omitempty"`

	// ExcludeNodes are nodes without the dedicated NIC — typically the control
	// plane.
	// +optional
	ExcludeNodes []string `json:"excludeNodes,omitempty"`

	// ExcludeIPs are ranges of ExternalCIDR kube-ovn must not allocate from.
	// +optional
	ExcludeIPs []string `json:"excludeIPs,omitempty"`

	// +kubebuilder:default=external
	// +optional
	ProviderNetworkName string `json:"providerNetworkName,omitempty"`

	// +kubebuilder:default=vlan-external
	// +optional
	VLANName string `json:"vlanName,omitempty"`

	// +kubebuilder:default=ext-sub
	// +optional
	SubnetName string `json:"subnetName,omitempty"`

	// KubeOVNNamespace is where the NAD and the link watcher go. Empty means
	// find it — the namespace running kube-ovn's own CNI DaemonSet.
	// +optional
	KubeOVNNamespace string `json:"kubeOVNNamespace,omitempty"`

	// LinkWatcher deploys the DaemonSet that keeps every provider NIC
	// administratively up.
	//
	// A workaround, and labelled as one. kube-ovn raises the provider NIC once
	// at bridge init and never rechecks it; on Talos it drops back DOWN minutes
	// later. A down provider NIC is the most misleading failure in this stack:
	// OVS lists the port, the bridge mapping is right, pods get addresses, and
	// every frame is swallowed.
	// +kubebuilder:default=true
	// +optional
	LinkWatcher *bool `json:"linkWatcher,omitempty"`

	// LinkWatcherImage overrides the image. Empty reuses the image kube-ovn's
	// own CNI DaemonSet runs — already pulled on exactly the nodes the watcher
	// runs on, and it carries iproute2 because kube-ovn uses it. A named public
	// image is a dependency that can rot: one such default stopped serving a
	// layer and the watcher sat in ImagePullBackOff, desired 3 ready 0, which
	// reads as a healthy DaemonSet in every summary that counts objects.
	// +optional
	LinkWatcherImage string `json:"linkWatcherImage,omitempty"`

	// CiliumSourceIPExempt deploys the DaemonSet that clears source-IP
	// verification on VPC gateway endpoints. Unset means decide by looking at
	// the cluster, which is the only answer that has ever been right: the
	// form's default of "no" was wrong on the one cluster it was asked about.
	//
	// Per-endpoint on purpose. The global `enable-source-ip-verification:
	// false` fixes it in one line and disables anti-spoofing for every pod in
	// the cluster — a bad trade when tenant worker VMs are root-accessible to
	// their tenants.
	// +optional
	CiliumSourceIPExempt *bool `json:"ciliumSourceIPExempt,omitempty"`

	// CiliumNamespace is where that DaemonSet goes. Empty means find it.
	// +optional
	CiliumNamespace string `json:"ciliumNamespace,omitempty"`

	// +kubebuilder:default="quay.io/cilium/cilium:v1.20.0"
	// +optional
	CiliumImage string `json:"ciliumImage,omitempty"`
}

// Condition types published by the underlay controller.
const (
	// ConditionFabricReady is false while any of the four objects the gateways
	// need is missing or refused.
	ConditionFabricReady = "FabricReady"

	// ConditionNodesLabelled is false when no node carries the provider NIC, or
	// when the label could not be restored on one that does.
	//
	// It is separate from FabricReady because it fails separately, and because
	// the failure is invisible: everything that must land on those nodes
	// selects on the label, so without it the link watcher is created,
	// schedules nowhere, and `kubectl rollout status` reports success — zero
	// desired pods are all ready.
	ConditionNodesLabelled = "NodesLabelled"

	// ConditionWorkaroundsRunning is false when a workaround DaemonSet exists
	// and is doing nothing: scheduled on no node, or scheduled and never
	// started.
	ConditionWorkaroundsRunning = "WorkaroundsRunning"
)

// UnderlayDaemonSetStatus is whether a DaemonSet is doing anything, as opposed
// to whether it exists.
type UnderlayDaemonSetStatus struct {
	Name      string `json:"name"`
	Namespace string `json:"namespace,omitempty"`
	// +optional
	Desired int32 `json:"desired"`
	// +optional
	Ready int32 `json:"ready"`
	// State is one of running, scheduled-nowhere, not-starting, absent, skipped.
	State string `json:"state"`
	// +optional
	Detail string `json:"detail,omitempty"`
}

// ManagedUnderlayStatus is what the fabric actually looks like.
type ManagedUnderlayStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// ReadyNodes are the nodes whose OVS bridge for this provider network came
	// up — kube-ovn's own answer to "does this NIC exist here".
	// +optional
	ReadyNodes []string `json:"readyNodes,omitempty"`

	// LabelledNodes carry `ovn.kubernetes.io/external-gw=true`.
	// +optional
	LabelledNodes []string `json:"labelledNodes,omitempty"`

	// LabelHeals counts how many times this controller has had to put the
	// gateway label back. It is not expected to be zero: on the lab the label
	// was found sitting at an explicit `false` on all three workers with
	// nothing in managedFields claiming it. A number that keeps climbing means
	// something else is still writing it, and is worth chasing; the fabric
	// stays up either way.
	// +optional
	LabelHeals int64 `json:"labelHeals,omitempty"`

	// Provider is the `<nad>.<namespace>.ovn` string tying the subnet to its
	// NAD, published because kube-ovn compares it character for character and
	// says so only in a refusal.
	// +optional
	Provider string `json:"provider,omitempty"`

	// DaemonSets is the state of the workarounds.
	// +optional
	DaemonSets []UnderlayDaemonSetStatus `json:"daemonSets,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=mul
// +kubebuilder:printcolumn:name="Interface",type=string,JSONPath=`.spec.interface`
// +kubebuilder:printcolumn:name="Subnet",type=string,JSONPath=`.spec.subnetName`
// +kubebuilder:printcolumn:name="Fabric",type=string,JSONPath=`.status.conditions[?(@.type=="FabricReady")].status`
// +kubebuilder:printcolumn:name="Nodes",type=string,JSONPath=`.status.conditions[?(@.type=="NodesLabelled")].status`
// +kubebuilder:printcolumn:name="Heals",type=integer,JSONPath=`.status.labelHeals`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ManagedUnderlay is the physical path out of the cluster.
//
// Its children carry ownerReferences, unlike a ManagedVM's. The reasoning is
// the opposite one: a VM must survive its CR being rolled back, because the
// workload is the point and the record of it is not. Here the objects *are*
// the record — a ProviderNetwork with no ManagedUnderlay behind it is fabric
// nobody is keeping up.
//
// The consequence is worth stating plainly: deleting this object takes the
// external subnet with it, and every egress gateway attached to it. That is
// not a subtle blast radius, but it is the honest one, and it is the same as
// deleting the Subnet by hand — which is exactly what used to happen, with no
// object anywhere saying what the four pieces had to do with each other.
type ManagedUnderlay struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec ManagedUnderlaySpec `json:"spec"`

	// +optional
	Status ManagedUnderlayStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ManagedUnderlayList contains a list of ManagedUnderlay.
type ManagedUnderlayList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedUnderlay `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion, &ManagedUnderlay{}, &ManagedUnderlayList{})
		return nil
	})
}
