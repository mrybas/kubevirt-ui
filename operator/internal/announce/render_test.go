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

package announce

import (
	"strings"
	"testing"
)

func sample() []Announcement {
	return []Announcement{
		{VPC: "tenant-b", CIDR: "10.200.24.0/22", NextHop: "10.199.4.9"},
		{VPC: "tenant-a", CIDR: "10.200.0.0/22", NextHop: "10.199.4.1"},
	}
}

// Each of these is silent when missing: the session comes up, the config looks
// healthy, and nothing is advertised. They are asserted individually because a
// diff against a golden string would not say which one went.
func TestTheThreeLinesThatFailQuietlyArePresent(t *testing.T) {
	got := RenderRawConfig(sample(), "10.198.175.254", 65030, 65000)

	if !strings.Contains(got, " no bgp ebgp-requires-policy") {
		t.Error("missing ebgp-requires-policy: the session would come up and advertise nothing")
	}
	if !strings.Contains(got, " no bgp network import-check") {
		t.Error("missing network import-check: every network statement would be ignored")
	}
	if !strings.Contains(got, "  neighbor 10.198.175.254 route-map B3-NH out") {
		t.Error("the next hop is not on the outbound neighbor route-map, " +
			"so it would be advertised as 0.0.0.0 and resolved to the node")
	}
}

// Two tenants advertising the same prefix length to the same peer need a branch
// each, or one next hop wins and the other tenant's traffic goes to it.
func TestEachPrefixGetsItsOwnNextHopBranch(t *testing.T) {
	got := RenderRawConfig(sample(), "10.198.175.254", 65030, 65000)

	for _, want := range []string{
		"ip prefix-list PL-TENANT-A seq 5 permit 10.200.0.0/22",
		"ip prefix-list PL-TENANT-B seq 5 permit 10.200.24.0/22",
		" set ip next-hop 10.199.4.1",
		" set ip next-hop 10.199.4.9",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q", want)
		}
	}
}

// A rewrite reloads FRR, and a reload is the one moment a session can flap. So
// the same input has to render the same bytes regardless of the order it
// arrives in.
func TestTheSameInputRendersTheSameBytes(t *testing.T) {
	forward := RenderRawConfig(sample(), "peer", 65030, 65000)

	reversed := sample()
	reversed[0], reversed[1] = reversed[1], reversed[0]
	backward := RenderRawConfig(reversed, "peer", 65030, 65000)

	if forward != backward {
		t.Fatalf("input order changed the output:\n---\n%s\n---\n%s", forward, backward)
	}
}

func TestNothingToAnnounceLeavesNoRouteMap(t *testing.T) {
	got := RenderRawConfig(nil, "peer", 65030, 65000)
	if strings.Contains(got, "route-map B3-NH out") {
		t.Fatal("an empty announcement set still points the peer at a route-map that has no branches")
	}
	// But the session is still configured: withdrawing everything is a state,
	// not an absence.
	if !strings.Contains(got, "neighbor peer remote-as 65000") {
		t.Fatal("withdrawing every prefix also dropped the session")
	}
}

// The datapath decides, not a label.
func TestOnlyADefaultRouteIntoTheExternalPlaneCounts(t *testing.T) {
	const external = "10.199.4.0/22"

	if !RoutedVia([]string{"10.199.4.254"}, external) {
		t.Error("a default route into the external plane should be announced")
	}
	if RoutedVia([]string{"10.199.0.5"}, external) {
		t.Error("a default route pointing at a gateway's transit address was announced; " +
			"that puts a second path to the prefix on the border while the working one is somebody else's")
	}
	if RoutedVia(nil, external) {
		t.Error("a VPC with no default route at all was announced")
	}
	if RoutedVia([]string{"not-an-address"}, external) {
		t.Error("an unparseable next hop was treated as routed")
	}
	if RoutedVia([]string{"10.199.4.254"}, "not-a-cidr") {
		t.Error("an unparseable external CIDR was treated as matching")
	}
}
