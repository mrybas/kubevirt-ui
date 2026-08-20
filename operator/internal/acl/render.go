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

// Package acl composes the rule list on a tenant subnet.
//
// The shape and the reasoning behind it are in docs/acl-composition.md. The
// short version: deny the tenant supernet once and carve the exceptions above
// it, rather than enumerating every other tenant — which is what made the list
// grow as the square of the tenant count.
package acl

import (
	"fmt"
	"net/netip"
	"sort"
	"strings"
)

// Priorities. The bands are the ones already on the cluster, kept exactly:
// changing a number here silently reorders live rules.
const (
	// PriorityMgmtDeny is above everything: this one is not an exception
	// anybody carves out.
	PriorityMgmtDeny = 3300
	// PriorityOwn lets a network talk to itself.
	PriorityOwn = 3200
	// PriorityException is where shared prefixes and peered networks sit. Each
	// shared prefix takes its own number upward from here so it stays
	// individually identifiable, and removable.
	PriorityException = 3100
	// PriorityDrop is the isolation floor.
	PriorityDrop = 3000
)

// Rule is one entry of Subnet.spec.acls.
type Rule struct {
	Action    string `json:"action"`
	Direction string `json:"direction"`
	Match     string `json:"match"`
	Priority  int    `json:"priority"`
}

// Input is everything the list is derived from.
type Input struct {
	// SubnetCIDR is the network being protected.
	SubnetCIDR string
	// Supernet is the aggregate every tenant network is carved from.
	Supernet string
	// TenantCIDRs is every tenant network in the cluster, this one included.
	// Used only for the containment check below.
	TenantCIDRs []string
	// PeerCIDRs are the networks this one is peered with — the truth about who
	// may talk, derived from the peerings rather than from the rules.
	PeerCIDRs []string
	// SharedCIDRs are prefixes reachable while isolated.
	SharedCIDRs []string
	// MgmtCIDRs are the management addresses that must not open connections
	// into a tenant.
	MgmtCIDRs []string
	// Manual are rules an operator asked for by hand.
	Manual []Rule
	// Isolated is whether the tenant-to-tenant floor applies at all.
	Isolated bool
}

// Render is the whole list, and the tenant networks that fall outside the
// supernet and therefore had to be denied individually.
//
// That second return value is the guard against the mistake this design already
// made once: the aggregate drop is only correct while the aggregate contains
// the tenants, and a deployment once had a supernet containing none of them. So
// containment is checked on every pass rather than configured and trusted.
func Render(in Input) (rules []Rule, outOfRange []string) {
	for _, cidr := range dedupe(in.MgmtCIDRs) {
		// One direction only, deliberately: this drops connections coming *at*
		// the tenant. Traffic the tenant starts towards the management network
		// is a different question and is not answered here.
		rules = append(rules, Rule{
			Action: "drop", Direction: "to-lport",
			Match: "ip4.src == " + cidr, Priority: PriorityMgmtDeny,
		})
	}

	if !in.Isolated {
		// Nothing below this line is about a network that asked to stay open.
		rules = append(rules, in.Manual...)
		return normalise(rules), nil
	}

	if in.SubnetCIDR != "" {
		rules = append(rules,
			Rule{Action: "allow-related", Direction: "from-lport",
				Match: "ip4.dst == " + in.SubnetCIDR, Priority: PriorityOwn},
			Rule{Action: "allow-related", Direction: "to-lport",
				Match: "ip4.src == " + in.SubnetCIDR, Priority: PriorityOwn},
		)
	}

	// Each shared prefix gets its own priority so it stays individually
	// identifiable rather than collapsing into one band with the others.
	priority := PriorityException
	for _, shared := range dedupe(in.SharedCIDRs) {
		rules = append(rules,
			Rule{Action: "allow-related", Direction: "from-lport",
				Match: "ip4.dst == " + shared, Priority: priority},
			Rule{Action: "allow-related", Direction: "to-lport",
				Match: "ip4.src == " + shared, Priority: priority},
		)
		priority++
	}

	for _, peer := range dedupe(in.PeerCIDRs) {
		if peer == in.SubnetCIDR {
			continue
		}
		rules = append(rules,
			Rule{Action: "allow-related", Direction: "from-lport",
				Match: "ip4.dst == " + peer, Priority: PriorityException},
			Rule{Action: "allow-related", Direction: "to-lport",
				Match: "ip4.src == " + peer, Priority: PriorityException},
		)
	}

	// The floor. One rule for the whole supernet instead of one per tenant.
	var floors []string
	if in.Supernet != "" {
		floors = append(floors, in.Supernet)
	}
	outOfRange = outside(in.Supernet, in.TenantCIDRs, in.SubnetCIDR)
	floors = append(floors, outOfRange...)

	// A drop with nothing to scope it to would take the internet with it, so
	// there is deliberately no fallback here: no scope, no isolation, and the
	// caller is told rather than handed a rule set that blackholes egress.
	for _, target := range dedupe(floors) {
		rules = append(rules,
			Rule{Action: "drop", Direction: "from-lport",
				Match: "ip4.dst == " + target, Priority: PriorityDrop},
			Rule{Action: "drop", Direction: "to-lport",
				Match: "ip4.src == " + target, Priority: PriorityDrop},
		)
	}

	rules = append(rules, in.Manual...)
	return normalise(rules), outOfRange
}

// outside is the containment check: tenant networks the aggregate does not
// cover, and therefore cannot deny.
func outside(supernet string, tenants []string, self string) []string {
	if supernet == "" {
		// Without an aggregate every other tenant has to be named individually,
		// which is the old shape — correct, just large.
		return dedupe(without(tenants, self))
	}
	aggregate, err := netip.ParsePrefix(supernet)
	if err != nil {
		return dedupe(without(tenants, self))
	}
	var out []string
	for _, cidr := range dedupe(without(tenants, self)) {
		prefix, err := netip.ParsePrefix(cidr)
		if err != nil {
			// Unparseable: name it rather than assume it is covered.
			out = append(out, cidr)
			continue
		}
		if !aggregate.Overlaps(prefix) || prefix.Bits() < aggregate.Bits() {
			out = append(out, cidr)
		}
	}
	return out
}

// Unaccounted is every live rule the composer did not produce.
//
// Adoption turns on this being empty. A rule nobody here can explain is not
// silently dropped and not silently kept: the subnet is left unmanaged and the
// rule is named, because taking ownership of a list means being able to
// reproduce all of it.
func Unaccounted(live, rendered []Rule) []Rule {
	known := map[Rule]int{}
	for _, r := range rendered {
		known[r]++
	}
	var out []Rule
	for _, r := range live {
		if known[r] > 0 {
			known[r]--
			continue
		}
		out = append(out, r)
	}
	return normalise(out)
}

// Equal compares two lists as sets. The API server does not promise an order
// and neither does anything that writes them.
func Equal(a, b []Rule) bool {
	x, y := normalise(a), normalise(b)
	if len(x) != len(y) {
		return false
	}
	for i := range x {
		if x[i] != y[i] {
			return false
		}
	}
	return true
}

// Describe renders a rule the way a person would read it in a refusal.
func Describe(r Rule) string {
	return fmt.Sprintf("%s %s %q @%d", r.Action, r.Direction, r.Match, r.Priority)
}

func normalise(rules []Rule) []Rule {
	out := append([]Rule(nil), rules...)
	sort.Slice(out, func(i, j int) bool {
		if out[i].Priority != out[j].Priority {
			return out[i].Priority > out[j].Priority
		}
		if out[i].Direction != out[j].Direction {
			return out[i].Direction < out[j].Direction
		}
		if out[i].Action != out[j].Action {
			return out[i].Action < out[j].Action
		}
		return out[i].Match < out[j].Match
	})
	return out
}

func dedupe(in []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, v := range in {
		v = strings.TrimSpace(v)
		if v == "" || seen[v] {
			continue
		}
		seen[v] = true
		out = append(out, v)
	}
	return out
}

func without(in []string, drop string) []string {
	var out []string
	for _, v := range in {
		if v != drop {
			out = append(out, v)
		}
	}
	return out
}
