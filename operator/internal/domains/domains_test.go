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

package domains

import "testing"

func TestParseRejectsUnknownDomain(t *testing.T) {
	// A typo must stop the process, not start a manager that watches nothing:
	// "no controller is running" is indistinguishable from "everything is
	// already reconciled" when you look at the cluster.
	if _, err := Parse("vm,netwrok"); err == nil {
		t.Fatal("expected a typo'd domain to be rejected, it was accepted")
	}
}

func TestParseRejectsEmptySelection(t *testing.T) {
	for _, in := range []string{"", "   ", ",,"} {
		if _, err := Parse(in); err == nil {
			t.Fatalf("expected %q to be rejected, it was accepted", in)
		}
	}
}

func TestParseAcceptsKnownDomains(t *testing.T) {
	got, err := Parse(" vm , tenant ")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !got.Has(VM) || !got.Has(Tenant) {
		t.Fatalf("expected vm and tenant enabled, got %q", got)
	}
	if got.Has(Network) || got.Has(Remediation) {
		t.Fatalf("expected network and remediation disabled, got %q", got)
	}
}

func TestLeaderElectionIDDiffersPerProfile(t *testing.T) {
	// Two profiles of the same image must not contend for one lease: whoever
	// lost the election would sit idle while its domain went unreconciled, and
	// nothing in either process would say so.
	vm, err := Parse("vm")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	network, err := Parse("network")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if vm.LeaderElectionID() == network.LeaderElectionID() {
		t.Fatalf("vm and network profiles share lease %q", vm.LeaderElectionID())
	}
}

func TestLeaderElectionIDIsOrderIndependent(t *testing.T) {
	// The same profile written in a different order is the same profile: a
	// restart with reordered args must rejoin its own lease, not fork a second.
	a, err := Parse("vm,network")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	b, err := Parse("network,vm")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if a.LeaderElectionID() != b.LeaderElectionID() {
		t.Fatalf("lease depends on argument order: %q vs %q",
			a.LeaderElectionID(), b.LeaderElectionID())
	}
}
