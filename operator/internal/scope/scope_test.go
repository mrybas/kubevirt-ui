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

package scope

import "testing"

func TestAGlobalNetworkIsUsableAnywhere(t *testing.T) {
	got := Check(Network{Name: "shared", VPC: "shared-vpc"}, Target{Folder: "opdev", Environment: "dev"})
	if !got.Allowed {
		t.Fatalf("a VPC with no folder must be global, got %+v", got)
	}
}

// This is the case measured on the stand: the wizard hid the network and the
// create path took it anyway.
func TestAnotherFoldersNetworkIsRefused(t *testing.T) {
	got := Check(
		Network{Name: "uat-net-vm-default", VPC: "uat-net-vm", Folder: "poc-transit", Environment: "dev"},
		Target{Folder: "opdev", Environment: "dev"},
	)
	if got.Allowed {
		t.Fatal("a VPC belonging to another folder was accepted")
	}
	if got.Reason != "NetworkOutOfScope" {
		t.Fatalf("reason = %q", got.Reason)
	}
	if !contains(got.Message, "poc-transit") || !contains(got.Message, "opdev") {
		t.Fatalf("message does not name both folders: %q", got.Message)
	}
}

func TestTheSameFolderButAnotherEnvironmentIsRefused(t *testing.T) {
	got := Check(
		Network{Name: "prod-net", VPC: "prod-vpc", Folder: "opdev", Environment: "prod"},
		Target{Folder: "opdev", Environment: "dev"},
	)
	if got.Allowed {
		t.Fatal("a VPC scoped to another environment was accepted")
	}
}

func TestAFolderScopedNetworkIsUsableInThatFolder(t *testing.T) {
	got := Check(
		Network{Name: "team-net", VPC: "team-vpc", Folder: "opdev"},
		Target{Folder: "opdev", Environment: "dev"},
	)
	if !got.Allowed {
		t.Fatalf("own folder network refused: %+v", got)
	}
}

func TestSystemNetworksAreNeverUsable(t *testing.T) {
	for _, n := range []Network{
		{Name: "join"},
		{Name: "ovn-default"},
		{Name: "something", VPC: SystemVPC},
	} {
		if Check(n, Target{Folder: "opdev"}).Allowed {
			t.Fatalf("%+v was accepted; attaching a VM there breaks cluster networking", n)
		}
	}
}

func TestInfrastructureSubnetsAreNotForVMs(t *testing.T) {
	got := Check(
		Network{Name: "gw-leg", VPC: "egress", Purpose: PurposeInfrastructure},
		Target{Folder: "opdev"},
	)
	if got.Allowed {
		t.Fatal("an infrastructure subnet was offered as a VM interface")
	}
}

func TestAVLANUnderlayIsNotFolderScoped(t *testing.T) {
	got := Check(Network{Name: "vlan-300", VLAN: "vlan-300"}, Target{Folder: "opdev"})
	if !got.Allowed {
		t.Fatalf("a plain VLAN underlay must not be folder-scoped: %+v", got)
	}
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
