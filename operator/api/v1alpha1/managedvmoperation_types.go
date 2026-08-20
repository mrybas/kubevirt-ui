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

// Operations a ManagedVMOperation can ask for.
const (
	// OperationRestore restores a VM from one of its snapshots.
	OperationRestore = "Restore"
	// OperationMigrate moves a running VM to another node.
	OperationMigrate = "Migrate"
	// OperationRollbackDisk puts one attached disk back to a snapshot.
	OperationRollbackDisk = "RollbackDisk"
)

// Operation phases.
const (
	OperationPhasePending   = "Pending"
	OperationPhaseRunning   = "Running"
	OperationPhaseSucceeded = "Succeeded"
	OperationPhaseFailed    = "Failed"
)

// RestoreSpec asks for a VM to be put back to a snapshot.
type RestoreSpec struct {
	// SnapshotName is the VirtualMachineSnapshot to restore from.
	// +kubebuilder:validation:MinLength=1
	SnapshotName string `json:"snapshotName"`
}

// RollbackDiskSpec asks for one attached disk to be put back to a snapshot.
//
// The disk is replaced rather than rewritten: a new one is built from the
// snapshot, the machine is pointed at it, and only then is the old one removed.
// The path this replaces deleted the claim first and created its replacement
// afterwards, so a process that died in between left a machine with no disk at
// all and nothing anywhere that knew what it had been.
type RollbackDiskSpec struct {
	// SnapshotName is the VolumeSnapshot to roll back to.
	// +kubebuilder:validation:MinLength=1
	SnapshotName string `json:"snapshotName"`
}

// MigrateSpec asks for a running VM to move.
type MigrateSpec struct {
	// TargetNode restricts where the machine may land. Empty lets the
	// scheduler choose.
	//
	// It becomes a selector on the migration object, not on the machine. The
	// path this replaces pinned the machine itself with a nodeSelector and
	// nothing ever removed it, so every migrated VM stayed welded to whichever
	// node it had been sent to — the first one it could not leave again.
	// +optional
	TargetNode string `json:"targetNode,omitempty"`
}

// ManagedVMOperationSpec is one thing to do to one machine, once.
//
// Operations are separate objects rather than fields on the VM because they
// have a life of their own: they take minutes, they can fail halfway, and what
// happened needs to be readable afterwards. Everything this replaces ran inside
// an HTTP request and died with it — including, in the case of a restore, the
// only record of whether the machine had been running before it started.
// +kubebuilder:validation:XValidation:rule="self.action != 'Restore' || has(self.restore)",message="a Restore operation needs a restore block"
// +kubebuilder:validation:XValidation:rule="self.action != 'Migrate' || has(self.migrate)",message="a Migrate operation needs a migrate block"
// +kubebuilder:validation:XValidation:rule="self.action != 'RollbackDisk' || has(self.rollbackDisk)",message="a RollbackDisk operation needs a rollbackDisk block"
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="an operation is a request to do something once; create a new one instead of editing this"
type ManagedVMOperationSpec struct {
	// VMName is the ManagedVM to act on, in this namespace.
	// +kubebuilder:validation:MinLength=1
	VMName string `json:"vmName"`

	// Action to perform.
	// +kubebuilder:validation:Enum=Restore;Migrate;RollbackDisk
	Action string `json:"action"`

	// +optional
	Restore *RestoreSpec `json:"restore,omitempty"`

	// +optional
	Migrate *MigrateSpec `json:"migrate,omitempty"`

	// +optional
	RollbackDisk *RollbackDiskSpec `json:"rollbackDisk,omitempty"`

	// TTLSecondsAfterFinished is how long a finished operation sticks around.
	// Long enough to read what happened, short enough that a busy namespace
	// does not fill with history.
	// +kubebuilder:default=3600
	// +kubebuilder:validation:Minimum=0
	// +optional
	TTLSecondsAfterFinished int32 `json:"ttlSecondsAfterFinished,omitempty"`
}

// ManagedVMOperationStatus is the state machine, kept on the object so it
// survives the process that started it.
type ManagedVMOperationStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Phase is Pending, Running, Succeeded or Failed.
	// +kubebuilder:validation:Enum=Pending;Running;Succeeded;Failed
	// +optional
	Phase string `json:"phase,omitempty"`

	// Message explains a failure, or what is being waited on.
	// +optional
	Message string `json:"message,omitempty"`

	// RunningBefore records whether the machine was running when the operation
	// started, so it can be put back that way afterwards.
	//
	// On the object, not in a variable: a restore that stopped the machine and
	// then lost its process left the machine stopped for good, and nothing
	// anywhere remembered that it should not have been.
	// +optional
	RunningBefore *bool `json:"runningBefore,omitempty"`

	// ChildName is the KubeVirt object doing the work — a
	// VirtualMachineRestore or a VirtualMachineInstanceMigration.
	// +optional
	ChildName string `json:"childName,omitempty"`

	// ReplacementDisk is the disk built from the snapshot during a rollback.
	//
	// Recorded before it is attached and kept until the old one is gone, so a
	// pass that resumes after a crash knows which disk it was in the middle of
	// swapping in rather than starting again and building a second one.
	// +optional
	ReplacementDisk string `json:"replacementDisk,omitempty"`

	// ReplacedDisk is the disk being rolled back, kept so it can be removed
	// once the machine is running on its replacement.
	// +optional
	ReplacedDisk string `json:"replacedDisk,omitempty"`

	// StartTime is when the operation began.
	// +optional
	StartTime *metav1.Time `json:"startTime,omitempty"`

	// CompletionTime is when it finished, either way.
	// +optional
	CompletionTime *metav1.Time `json:"completionTime,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// Finished reports whether the operation has reached a terminal phase.
func (s *ManagedVMOperationStatus) Finished() bool {
	return s.Phase == OperationPhaseSucceeded || s.Phase == OperationPhaseFailed
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=mvmop
// +kubebuilder:printcolumn:name="VM",type=string,JSONPath=`.spec.vmName`
// +kubebuilder:printcolumn:name="Action",type=string,JSONPath=`.spec.action`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Message",type=string,JSONPath=`.status.message`,priority=1
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ManagedVMOperation is one operation on one machine.
type ManagedVMOperation struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +required
	Spec ManagedVMOperationSpec `json:"spec"`

	// +optional
	Status ManagedVMOperationStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ManagedVMOperationList contains a list of ManagedVMOperation.
type ManagedVMOperationList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ManagedVMOperation `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(SchemeGroupVersion, &ManagedVMOperation{}, &ManagedVMOperationList{})
		return nil
	})
}
