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

// Package scope answers one question — may this namespace use this network —
// and is the only place that answers it.
//
// It exists because the answer was already given in two places that disagreed.
// The wizard hides a VPC belonging to another folder; the create path accepted
// it. Measured on the stand: a VM in folder `opdev` attached to `uat-net-vm`,
// a VPC scoped to folder `poc-transit`, which the wizard correctly refused to
// offer. Whichever rule is right, there has to be one of it.
package scope

// PurposeLabel marks a subnet reserved for infrastructure — a NAT gateway leg,
// a transit plane — rather than for workloads.
const PurposeLabel = "kubevirt-ui.io/purpose"

// PurposeInfrastructure is the value that keeps a subnet out of VM pickers.
const PurposeInfrastructure = "infrastructure"

// SystemVPC is kube-ovn's own VPC. Attaching a VM to its subnets breaks
// cluster networking.
const SystemVPC = "ovn-cluster"

// systemSubnets are kube-ovn's own, never offered and never accepted.
var systemSubnets = map[string]struct{}{
	"join":        {},
	"ovn-default": {},
}

// Network is what the decision is made about.
type Network struct {
	// Name of the subnet.
	Name string
	// VPC the subnet belongs to, empty for a plain VLAN-backed underlay.
	VPC string
	// VLAN backing the subnet, empty for a VPC overlay.
	VLAN string
	// Folder and Environment the VPC is scoped to, read from its labels.
	// Empty folder means a global network, usable from anywhere.
	Folder      string
	Environment string
	// Purpose is the subnet's purpose label.
	Purpose string
}

// Target is the namespace that wants to attach.
type Target struct {
	Folder      string
	Environment string
}

// Result explains a refusal. Allowed results carry no reason.
type Result struct {
	Allowed bool
	Reason  string
	Message string
}

func allow() Result { return Result{Allowed: true} }

// Check decides whether a VM in the target namespace may attach to the network.
//
// The rules, in the order they were learned:
//
//   - kube-ovn's own VPC and its system subnets are never usable; a VM attached
//     to them takes cluster networking down with it.
//   - a subnet marked as infrastructure carries a gateway or a transit plane,
//     not workloads.
//   - a VPC with no folder is global and usable from anywhere.
//   - otherwise the folder must match, and if the VPC is also scoped to one
//     environment, the environment must match too. A VPC belonging to another
//     folder offered to a VM being created here is another team's network.
func Check(net Network, target Target) Result {
	if _, system := systemSubnets[net.Name]; system || net.VPC == SystemVPC {
		return Result{
			Allowed: false,
			Reason:  "NetworkIsSystemOwned",
			Message: "subnet " + net.Name + " belongs to kube-ovn itself and cannot carry workloads",
		}
	}
	if net.Purpose == PurposeInfrastructure {
		return Result{
			Allowed: false,
			Reason:  "NetworkIsInfrastructure",
			Message: "subnet " + net.Name + " is reserved for infrastructure, not for VM interfaces",
		}
	}

	// A VLAN-backed underlay with no VPC is not folder-scoped.
	if net.VPC == "" || net.Folder == "" {
		return allow()
	}

	if net.Folder != target.Folder {
		return Result{
			Allowed: false,
			Reason:  "NetworkOutOfScope",
			Message: "subnet " + net.Name + " belongs to folder " + net.Folder +
				" and this namespace is in folder " + orNone(target.Folder),
		}
	}
	if net.Environment != "" && net.Environment != target.Environment {
		return Result{
			Allowed: false,
			Reason:  "NetworkOutOfScope",
			Message: "subnet " + net.Name + " is scoped to environment " + net.Environment +
				" and this namespace is environment " + orNone(target.Environment),
		}
	}
	return allow()
}

func orNone(s string) string {
	if s == "" {
		return "(none)"
	}
	return s
}
