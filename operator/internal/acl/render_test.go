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

package acl

import (
	"encoding/json"
	"net/netip"
	"os"
	"path/filepath"
	"testing"
)

// liveSubnet is a tenant subnet's rules as they exist on the stand.
type liveSubnet struct {
	CIDR string `json:"cidr"`
	ACLs []Rule `json:"acls"`
}

func loadLiveACLs(t *testing.T) liveSubnet {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "live-acls-uat-net-t1.json"))
	if err != nil {
		t.Fatalf("reading the fixture: %v", err)
	}
	var out liveSubnet
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("parsing the fixture: %v", err)
	}
	if len(out.ACLs) == 0 || out.CIDR == "" {
		t.Fatal("the fixture is empty and would prove nothing")
	}
	return out
}

// standInput is the stand, as the composer sees it.
func standInput() Input {
	return Input{
		SubnetCIDR: "10.200.4.0/22",
		Supernet:   "10.200.0.0/14",
		TenantCIDRs: []string{
			"10.200.0.0/22", "10.200.4.0/22", "10.200.8.0/22",
			"10.200.12.0/22", "10.200.24.0/22",
		},
		MgmtCIDRs: []string{
			"10.198.160.1/32", "10.198.160.2/32", "10.198.160.3/32",
			"10.198.160.4/32", "10.198.160.5/32", "10.198.160.6/32",
		},
		Isolated: true,
	}
}

// probes are the addresses the two rule sets must agree about.
func probes(t *testing.T) map[string]netip.Addr {
	t.Helper()
	out := map[string]netip.Addr{}
	for name, raw := range map[string]string{
		"own network":            "10.200.4.9",
		"another tenant":         "10.200.8.9",
		"a third tenant":         "10.200.0.9",
		"a tenant added later":   "10.200.24.9",
		"a management node":      "10.198.160.3",
		"another management box": "10.198.175.254",
		"the internet":           "8.8.8.8",
		"an unallocated tenant":  "10.203.255.1",
	} {
		addr, err := netip.ParseAddr(raw)
		if err != nil {
			t.Fatalf("bad probe %s: %v", name, err)
		}
		out[name] = addr
	}
	return out
}

// TestTheAggregateNeverAllowsWhatTheEnumerationDenied is the acceptance for the
// shape change.
//
// Replacing five enumerated peer drops with one aggregate rewrites every line,
// so comparing lines proves nothing. The property that must hold is one-sided:
// nothing the enumeration dropped may become allowed, and anything newly
// dropped must be inside the tenant aggregate — never the internet.
//
// It is one-sided rather than exact because the enumeration is a snapshot and
// the aggregate is a rule. Written against the stand's own list, this test
// reports the difference: `10.200.24.9` — a network created after the last
// isolation pass — and `10.203.255.1` — an address inside the supernet nobody
// has been given yet — are both reachable under the enumeration and denied
// under the aggregate. That is the hole the enumeration has by construction: a
// tenant is exposed to every other tenant from the moment it exists until
// something re-runs the pass on all of them.
func TestTheAggregateNeverAllowsWhatTheEnumerationDenied(t *testing.T) {
	live := loadLiveACLs(t)
	rendered, outOfRange := Render(standInput())

	if len(outOfRange) != 0 {
		t.Fatalf("every tenant network is inside the supernet on this stand, "+
			"yet these were reported outside: %v", outOfRange)
	}

	supernet := netip.MustParsePrefix("10.200.0.0/14")
	tightened := 0
	for name, source := range probes(t) {
		for _, direction := range []string{"to-lport", "from-lport"} {
			was := Evaluate(live.ACLs, source, direction)
			now := Evaluate(rendered, source, direction)
			switch {
			case was == now:
			case was == Dropped && now == Allowed:
				t.Errorf("%s (%s, %s) was denied and is now allowed",
					name, source, direction)
			default:
				// Newly denied. Only acceptable inside the tenant aggregate:
				// anywhere else and the floor has taken the internet with it.
				if !supernet.Contains(source) {
					t.Errorf("%s (%s, %s) is newly denied and is outside %s",
						name, source, direction, supernet)
					continue
				}
				tightened++
				t.Logf("newly denied, and rightly: %s (%s, %s) — inside the "+
					"tenant aggregate, and reachable today only because the "+
					"enumeration has not been re-run since it appeared",
					name, source, direction)
			}
		}
	}
	if tightened == 0 {
		t.Log("nothing tightened on this fixture")
	}

	// And the point of the exercise.
	if len(rendered) >= len(live.ACLs) {
		t.Errorf("the composed list is not smaller: %d vs %d live",
			len(rendered), len(live.ACLs))
	}
	t.Logf("%d rules become %d", len(live.ACLs), len(rendered))
}

// TestTheListStopsGrowingWithTheTenantCount is the reason for the change: the
// enumeration is 2·(N−1) rows per subnet, so the cluster carries 2·N·(N−1).
func TestTheListStopsGrowingWithTheTenantCount(t *testing.T) {
	sizes := map[int]int{}
	for _, tenants := range []int{5, 50, 400} {
		in := standInput()
		in.TenantCIDRs = nil
		for i := 0; i < tenants; i++ {
			in.TenantCIDRs = append(in.TenantCIDRs,
				netip.PrefixFrom(netip.AddrFrom4([4]byte{10, 200, byte(i / 64 * 4), 0}), 22).String())
		}
		rendered, _ := Render(in)
		sizes[tenants] = len(rendered)
	}
	if sizes[5] != sizes[400] {
		t.Errorf("the list grew with the tenant count: %v", sizes)
	}
}

// TestANetworkOutsideTheSupernetIsStillDenied guards the mistake this design
// already made once: the aggregate drop is only correct while the aggregate
// contains the tenants, and a deployment once had a supernet containing none of
// them. Two "Isolated" networks then had one rule between them naming a range
// holding neither.
func TestANetworkOutsideTheSupernetIsStillDenied(t *testing.T) {
	in := standInput()
	in.TenantCIDRs = append(in.TenantCIDRs, "10.100.0.0/24")

	rendered, outOfRange := Render(in)
	if len(outOfRange) != 1 || outOfRange[0] != "10.100.0.0/24" {
		t.Fatalf("outOfRange = %v", outOfRange)
	}

	stray := netip.MustParseAddr("10.100.0.9")
	if got := Evaluate(rendered, stray, "to-lport"); got != Dropped {
		t.Errorf("a tenant outside the supernet is reachable: %s", got)
	}
	// And the aggregate still does its job for everybody inside.
	inside := netip.MustParseAddr("10.200.8.9")
	if got := Evaluate(rendered, inside, "to-lport"); got != Dropped {
		t.Errorf("a tenant inside the supernet is reachable: %s", got)
	}
}

// TestAPeeredNetworkIsAllowedThroughTheFloor: peerings are the truth about who
// may talk, and the allow has to sit above the drop or it does nothing.
func TestAPeeredNetworkIsAllowedThroughTheFloor(t *testing.T) {
	in := standInput()
	in.PeerCIDRs = []string{"10.200.8.0/22"}

	rendered, _ := Render(in)
	peer := netip.MustParseAddr("10.200.8.9")
	for _, direction := range []string{"to-lport", "from-lport"} {
		if got := Evaluate(rendered, peer, direction); got != Allowed {
			t.Errorf("%s: a peered network is blocked: %s", direction, got)
		}
	}
	// Everybody else still is not.
	other := netip.MustParseAddr("10.200.12.9")
	if got := Evaluate(rendered, other, "to-lport"); got != Dropped {
		t.Errorf("an unpeered network got through: %s", got)
	}
}

// TestTheInternetIsNeverCaught: the floor is scoped to the tenant aggregate for
// exactly this reason. `Subnet.spec.private` is the obvious-looking knob and it
// takes egress with it.
func TestTheInternetIsNeverCaught(t *testing.T) {
	rendered, _ := Render(standInput())
	for _, raw := range []string{"8.8.8.8", "1.1.1.1", "10.199.4.254"} {
		addr := netip.MustParseAddr(raw)
		for _, direction := range []string{"to-lport", "from-lport"} {
			if got := Evaluate(rendered, addr, direction); got != Allowed {
				t.Errorf("%s (%s) is blocked: %s", raw, direction, got)
			}
		}
	}
}

// TestManagementCannotOpenConnectionsIn, and only in that direction: traffic the
// tenant starts towards the management network is a different question.
func TestManagementCannotOpenConnectionsIn(t *testing.T) {
	rendered, _ := Render(standInput())
	node := netip.MustParseAddr("10.198.160.3")
	if got := Evaluate(rendered, node, "to-lport"); got != Dropped {
		t.Errorf("a node can open connections into the tenant: %s", got)
	}
	if got := Evaluate(rendered, node, "from-lport"); got != Allowed {
		t.Errorf("the tenant cannot reach the management network: %s", got)
	}
}

// TestAnUnisolatedNetworkKeepsOnlyTheBaseline: "not isolated" is a statement
// about other tenants. The management boundary is not an exception anybody
// carves out.
func TestAnUnisolatedNetworkKeepsOnlyTheBaseline(t *testing.T) {
	in := standInput()
	in.Isolated = false

	rendered, _ := Render(in)
	for _, rule := range rendered {
		if rule.Priority != PriorityMgmtDeny {
			t.Errorf("unexpected rule on an un-isolated network: %s", Describe(rule))
		}
	}
	if got := Evaluate(rendered, netip.MustParseAddr("10.200.8.9"), "to-lport"); got != Allowed {
		t.Errorf("an un-isolated network is isolated anyway: %s", got)
	}
	if got := Evaluate(rendered, netip.MustParseAddr("10.198.160.3"), "to-lport"); got != Dropped {
		t.Errorf("the management boundary was dropped along with the isolation: %s", got)
	}
}

// TestAdoptionNamesWhatItCannotExplain. Taking ownership of a list means being
// able to reproduce all of it; a rule nobody here wrote is named and the subnet
// is left alone, because silently dropping somebody's rule is worse than
// declining to manage it.
func TestAdoptionNamesWhatItCannotExplain(t *testing.T) {
	rendered, _ := Render(standInput())

	if left := Unaccounted(rendered, rendered); len(left) != 0 {
		t.Fatalf("the composer cannot account for its own output: %v", left)
	}

	foreign := Rule{
		Action: "allow-related", Direction: "to-lport",
		Match: "ip4.src == 192.0.2.0/24", Priority: 2900,
	}
	live := append(append([]Rule(nil), rendered...), foreign)
	left := Unaccounted(live, rendered)
	if len(left) != 1 || left[0] != foreign {
		t.Fatalf("unaccounted = %v", left)
	}
}

// TestEqualIgnoresOrder: nothing promises the order of this list, and comparing
// it as a sequence would report a difference on every reshuffle.
func TestEqualIgnoresOrder(t *testing.T) {
	rendered, _ := Render(standInput())
	shuffled := append([]Rule(nil), rendered...)
	for i, j := 0, len(shuffled)-1; i < j; i, j = i+1, j-1 {
		shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
	}
	if !Equal(rendered, shuffled) {
		t.Error("the same set read in a different order is reported as different")
	}
	if Equal(rendered, rendered[1:]) {
		t.Error("a missing rule is reported as equal")
	}
}

// TestNoRuleSetIsAmbiguous. An allow and a drop at the same priority is not a
// decision OVN makes deterministically — it picks one, and which one is not
// specified. A composer that emits that pair has produced traffic that works on
// some nodes and not others, which is the worst kind of network bug to be
// handed.
func TestNoRuleSetIsAmbiguous(t *testing.T) {
	for name, in := range map[string]Input{
		"the stand":   standInput(),
		"with peers":  withPeers(standInput(), "10.200.8.0/22", "10.200.12.0/22"),
		"with shared": withShared(standInput(), "10.1.0.0/16"),
		"out of range": withTenants(standInput(),
			"10.200.0.0/22", "10.100.0.0/24", "10.200.4.0/22"),
	} {
		rendered, _ := Render(in)
		for probe, source := range probes(t) {
			for _, direction := range []string{"to-lport", "from-lport"} {
				if got := Evaluate(rendered, source, direction); got == Conflicted {
					t.Errorf("%s: %s (%s, %s) has no defined answer",
						name, probe, source, direction)
				}
			}
		}
	}
}

func withPeers(in Input, peers ...string) Input     { in.PeerCIDRs = peers; return in }
func withShared(in Input, shared ...string) Input   { in.SharedCIDRs = shared; return in }
func withTenants(in Input, tenants ...string) Input { in.TenantCIDRs = tenants; return in }

// TestAnInfrastructureNetworkTakesNoFloor. It serves the others, so it is not
// one of them. This is not a nicety: the isolation pass had already written
// tenant drops and the management baseline onto a shared gateway's own subnet,
// and a pod on a hub tenant's subnet lost the internet for it.
func TestAnInfrastructureNetworkTakesNoFloor(t *testing.T) {
	in := standInput()
	in.Role = "infrastructure"

	rendered, outOfRange := Render(in)
	if len(rendered) != 0 || len(outOfRange) != 0 {
		t.Fatalf("rendered %v / %v onto an infrastructure network", rendered, outOfRange)
	}

	// Everything reaches it, which is the point of it existing.
	for _, raw := range []string{"10.200.8.9", "10.198.160.3", "8.8.8.8"} {
		addr := netip.MustParseAddr(raw)
		if got := Evaluate(rendered, addr, "to-lport"); got != Allowed {
			t.Errorf("%s is blocked on an infrastructure network: %s", raw, got)
		}
	}

	// And a rule somebody asked for by hand survives.
	in.Manual = []Rule{{
		Action: "drop", Direction: "to-lport",
		Match: "ip4.src == 192.0.2.0/24", Priority: 2900,
	}}
	withManual, _ := Render(in)
	if len(withManual) != 1 {
		t.Fatalf("manual rules were dropped: %v", withManual)
	}
}
