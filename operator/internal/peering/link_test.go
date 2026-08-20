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

package peering

import (
	"fmt"
	"testing"
)

// TestAFreedLinkComesBack is the reason allocation is derived rather than
// counted. A counter only moves forward, so a link freed by deleting a peering
// is never handed out again: with sixty-two in the range, a long-lived cluster
// reports the pool exhausted while most of it is idle.
func TestAFreedLinkComesBack(t *testing.T) {
	used := []string{"169.254.101.0/30", "169.254.101.4/30", "169.254.101.8/30"}

	next, err := Allocate(used)
	if err != nil {
		t.Fatalf("allocate: %v", err)
	}
	if next.CIDR != "169.254.101.12/30" {
		t.Fatalf("next = %s", next.CIDR)
	}

	// The middle one is released.
	freed := []string{"169.254.101.0/30", "169.254.101.8/30"}
	again, err := Allocate(freed)
	if err != nil {
		t.Fatalf("allocate: %v", err)
	}
	if again.CIDR != "169.254.101.4/30" {
		t.Fatalf("a freed link was not reused: got %s", again.CIDR)
	}
}

// TestTheWholeRangeIsUsableAndThenSaysSo.
func TestTheWholeRangeIsUsableAndThenSaysSo(t *testing.T) {
	var used []string
	for i := 0; i < 62; i++ {
		link, err := Allocate(used)
		if err != nil {
			t.Fatalf("exhausted after %d links: %v", i, err)
		}
		used = append(used, link.CIDR)
	}
	if _, err := Allocate(used); err == nil {
		t.Fatal("a 63rd link was allocated out of a range that holds 62")
	}
}

// TestBothEndsAreDistinctAndInsideTheLink.
func TestBothEndsAreDistinctAndInsideTheLink(t *testing.T) {
	link, err := Parse("169.254.101.8/30")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if link.A == link.B {
		t.Fatal("both routers were handed the same address")
	}
	if link.A != "169.254.101.9" || link.B != "169.254.101.10" {
		t.Fatalf("ends = %s / %s", link.A, link.B)
	}
}

// TestALinkWithNoRoomIsRefused. A /31 has two addresses and no usable hosts in
// this model; a /32 has none at all.
func TestALinkWithNoRoomIsRefused(t *testing.T) {
	for _, cidr := range []string{"169.254.101.8/31", "169.254.101.8/32"} {
		if _, err := Parse(cidr); err == nil {
			t.Errorf("%s was accepted", cidr)
		}
	}
}

// TestTheRouteAndThePolicyTravelTogether. The routes alone are not the peering:
// without a policy route above the egress gateway's catch-all the traffic
// hairpins out to the upstream router anyway, and the peering looks broken for
// no visible reason — the link is up, the routes are there, and nothing goes
// through it.
func TestTheRouteAndThePolicyTravelTogether(t *testing.T) {
	side := RenderSide("other", "169.254.101.9", "169.254.101.8/30",
		[]string{"10.200.4.0/22", "10.200.8.0/22"})

	if len(side.Routes) != 2 || len(side.Policies) != 2 {
		t.Fatalf("routes %d, policies %d", len(side.Routes), len(side.Policies))
	}
	for i, route := range side.Routes {
		// The next hop is the *other* end of the link, not this one.
		if route["nextHopIP"] != "169.254.101.10" {
			t.Errorf("route %d points at %v", i, route["nextHopIP"])
		}
	}
	for i, policy := range side.Policies {
		if policy["priority"] != int64(PolicyPriority) {
			t.Errorf("policy %d at priority %v — below the gateway catch-all it "+
				"does nothing", i, policy["priority"])
		}
	}
	if side.Peering["localConnectIP"] != "169.254.101.9/30" {
		t.Errorf("connect address = %v", side.Peering["localConnectIP"])
	}
}

// TestEachEndPointsAtTheOther, for both halves of the same link.
func TestEachEndPointsAtTheOther(t *testing.T) {
	link, _ := Parse("169.254.101.16/30")
	for _, tc := range []struct{ connect, expect string }{
		{link.A, link.B},
		{link.B, link.A},
	} {
		side := RenderSide("peer", tc.connect, link.CIDR, []string{"10.0.0.0/8"})
		if got := side.Routes[0]["nextHopIP"]; got != tc.expect {
			t.Errorf("from %s the next hop is %v, want %s", tc.connect, got, tc.expect)
		}
	}
}

// TestUsedLinksReadsTheNetworkNotTheAddress: the entries carry an address with
// a prefix, and the thing that is taken is the /30 it sits in.
func TestUsedLinksReadsTheNetworkNotTheAddress(t *testing.T) {
	got := UsedLinks([]string{
		"169.254.101.9/30", "169.254.101.10/30", "169.254.101.5/30", "nonsense",
	})
	want := []string{"169.254.101.4/30", "169.254.101.8/30"}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}
