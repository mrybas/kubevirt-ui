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

// Package peering renders the two halves of a VPC peering.
package peering

import (
	"fmt"
	"net/netip"
	"sort"
	"strings"
)

const (
	// LinkBase is where point-to-point links are carved from. Link-local by
	// design: these addresses exist only between two routers and are never
	// routed, announced, or reachable from anywhere else.
	LinkBase = "169.254.101.0/24"

	// PolicyPriority puts the peering above the egress gateway's catch-all
	// reroute. Without it the traffic hairpins out to the upstream router
	// anyway and the peering looks broken for no visible reason — the routes
	// are there, the link is up, and nothing goes through it.
	PolicyPriority = 29000
)

// Link is one point-to-point subnet and the address each end holds.
type Link struct {
	CIDR string
	A    string
	B    string
}

// Parse turns a chosen CIDR into a link, refusing one with no room.
func Parse(cidr string) (Link, error) {
	prefix, err := netip.ParsePrefix(cidr)
	if err != nil {
		return Link{}, fmt.Errorf("%q is not a CIDR: %w", cidr, err)
	}
	prefix = prefix.Masked()
	if prefix.Bits() > 30 {
		return Link{}, fmt.Errorf("%s has no room for two addresses", cidr)
	}
	first := prefix.Addr().Next()
	return Link{CIDR: prefix.String(), A: first.String(), B: first.Next().String()}, nil
}

// Allocate picks the lowest /30 nobody is using.
//
// Derived from the links actually in use rather than from a counter. A counter
// only moves forward, so a link freed by deleting a peering is never handed out
// again and the pool reports itself exhausted while most of it is idle — with
// sixty-two links in the range, that is a wall a long-lived cluster reaches
// while doing nothing unusual.
//
// The race a counter was there to prevent is a different problem with a
// different answer: two allocations at the same moment. One writer under leader
// election cannot race itself, and the caller re-reads afterwards to catch
// anything that was not it.
func Allocate(used []string) (Link, error) {
	taken := map[string]bool{}
	for _, cidr := range used {
		if prefix, err := netip.ParsePrefix(strings.TrimSpace(cidr)); err == nil {
			taken[prefix.Masked().String()] = true
		}
	}

	base := netip.MustParsePrefix(LinkBase)
	addr := base.Addr()
	for i := 0; i < 62; i++ {
		candidate := netip.PrefixFrom(addr, 30).Masked()
		if !taken[candidate.String()] {
			return Parse(candidate.String())
		}
		for step := 0; step < 4; step++ {
			addr = addr.Next()
		}
		if !base.Contains(addr) {
			break
		}
	}
	return Link{}, fmt.Errorf(
		"every /30 in %s is in use by another peering", LinkBase)
}

// UsedLinks reads the link addresses out of a set of peering entries.
//
// The entries carry `localConnectIP` as `<address>/<prefix>`; the link is the
// network that address sits in.
func UsedLinks(connectIPs []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, raw := range connectIPs {
		prefix, err := netip.ParsePrefix(strings.TrimSpace(raw))
		if err != nil {
			continue
		}
		network := prefix.Masked().String()
		if seen[network] {
			continue
		}
		seen[network] = true
		out = append(out, network)
	}
	sort.Strings(out)
	return out
}

// Side is what one router's spec has to say about the peering.
type Side struct {
	// Peering is the entry in `spec.vpcPeerings`.
	Peering map[string]any
	// Routes are the static routes to the other network's prefixes.
	Routes []map[string]any
	// Policies sit above the egress gateway's catch-all so the traffic takes
	// the link instead of hairpinning.
	Policies []map[string]any
}

// RenderSide builds one end of the peering.
func RenderSide(remote, connectIP, linkCIDR string, remoteCIDRs []string) Side {
	prefix := linkCIDR
	if index := strings.LastIndex(linkCIDR, "/"); index >= 0 {
		prefix = linkCIDR[index+1:]
	}
	side := Side{
		Peering: map[string]any{
			"remoteVpc":      remote,
			"localConnectIP": connectIP + "/" + prefix,
		},
	}
	for _, cidr := range remoteCIDRs {
		side.Routes = append(side.Routes, map[string]any{
			"cidr": cidr, "nextHopIP": otherEnd(connectIP, linkCIDR), "policy": "policyDst",
		})
		side.Policies = append(side.Policies, map[string]any{
			"priority": int64(PolicyPriority),
			"action":   "allow",
			"match":    "ip4.dst == " + cidr,
		})
	}
	return side
}

// otherEnd is the address at the far end of the same /30.
func otherEnd(connectIP, linkCIDR string) string {
	link, err := Parse(linkCIDR)
	if err != nil {
		return ""
	}
	if connectIP == link.A {
		return link.B
	}
	return link.A
}
