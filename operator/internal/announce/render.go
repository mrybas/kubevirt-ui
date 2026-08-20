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

// Package announce renders the BGP configuration that tells the border how to
// reach each tenant network.
//
// The tenant's own router has a leg on the external VLAN, and the border learns
// the tenant's prefix with **that leg** as the next hop. Nothing in the data
// path depends on a pod staying alive.
//
// The raw form is not a preference. frr-k8s exposes no next-hop field in its
// structured API, and the next hop is the one thing this has to say. Three
// lines in the raw config were each measured, and each fails silently when
// missing:
//
//   - `no bgp ebgp-requires-policy` — without it FRR brings the session up
//     Established and advertises nothing; the only hint is `(Policy)` in
//     `show bgp ipv4 summary`.
//   - `no bgp network import-check` — a `network` statement is ignored unless
//     the prefix is in the node's RIB, and a tenant prefix never is.
//   - the next hop must be set on the outbound **neighbor** route-map. Set on
//     `network <cidr> route-map X` instead, it advertises next-hop 0.0.0.0,
//     which the border resolves to the node — the announcement then looks
//     accepted while pointing at the wrong place.
package announce

import (
	"fmt"
	"net/netip"
	"sort"
	"strings"
)

// Announcement is one tenant prefix and the router leg to send it to.
type Announcement struct {
	VPC     string
	CIDR    string
	NextHop string
}

// Slug is the prefix-list name fragment. FRR wants no dots or slashes.
func Slug(vpc string) string {
	var b strings.Builder
	for _, r := range vpc {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-':
			b.WriteRune(r)
		default:
			b.WriteRune('-')
		}
	}
	return strings.ToUpper(b.String())
}

// RenderRawConfig produces the FRR snippet.
//
// Deterministic on purpose: an unchanged input must render byte-identical, or
// every pass rewrites the object and every rewrite reloads FRR — and a reload
// is the one moment a session can flap.
func RenderRawConfig(announcements []Announcement, peer string, asn, remoteASN int32) string {
	ordered := make([]Announcement, len(announcements))
	copy(ordered, announcements)
	sort.Slice(ordered, func(i, j int) bool {
		if ordered[i].VPC != ordered[j].VPC {
			return ordered[i].VPC < ordered[j].VPC
		}
		return ordered[i].CIDR < ordered[j].CIDR
	})

	lines := []string{
		fmt.Sprintf("router bgp %d", asn),
		// Each of the next two is silent when missing — see the package comment.
		" no bgp ebgp-requires-policy",
		" no bgp network import-check",
		fmt.Sprintf(" neighbor %s remote-as %d", peer, remoteASN),
		fmt.Sprintf(" neighbor %s timers 10 30", peer),
		" address-family ipv4 unicast",
	}
	for _, a := range ordered {
		lines = append(lines, fmt.Sprintf("  network %s", a.CIDR))
	}
	if len(ordered) > 0 {
		// The next hop belongs here and nowhere else.
		lines = append(lines, fmt.Sprintf("  neighbor %s route-map B3-NH out", peer))
	}
	lines = append(lines, " exit-address-family", "!")

	for _, a := range ordered {
		lines = append(lines, fmt.Sprintf("ip prefix-list PL-%s seq 5 permit %s", Slug(a.VPC), a.CIDR))
	}
	lines = append(lines, "!")

	for i, a := range ordered {
		lines = append(lines,
			fmt.Sprintf("route-map B3-NH permit %d", (i+1)*10),
			fmt.Sprintf(" match ip address prefix-list PL-%s", Slug(a.VPC)),
			fmt.Sprintf(" set ip next-hop %s", a.NextHop),
		)
	}

	return strings.Join(lines, "\n") + "\n"
}

// RoutedVia reports whether a VPC's default route hands traffic to the external
// plane.
//
// This is the marker of "announce me": it reads the datapath rather than a
// label somebody has to remember to set, so it cannot drift away from reality.
// A leg on the external network is not sufficient on its own — an egress
// gateway's own VPC has one, and so does every tenant still routed through a
// gateway, whose traffic leaves from somewhere else entirely. Announcing those
// puts a second, competing path to the same prefix on the border while the
// working one belongs to somebody else. Measured: the first run of the
// generator this replaces offered six prefixes, four of which had no business
// being there.
//
// It is exported because the egress guard has to ask the same question, and two
// implementations of it would eventually answer differently.
func RoutedVia(defaultRouteNextHops []string, externalCIDR string) bool {
	network, err := netip.ParsePrefix(externalCIDR)
	if err != nil {
		return false
	}
	network = network.Masked()
	for _, hop := range defaultRouteNextHops {
		addr, err := netip.ParseAddr(hop)
		if err != nil {
			continue
		}
		if network.Contains(addr) {
			return true
		}
	}
	return false
}
