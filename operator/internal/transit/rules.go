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

// Package transit is the control-plane transit plane: the leg a tenant's
// workers reach their own control plane over, and the rules that keep one
// tenant's leg out of another's.
//
// The plane exists so that reaching a control plane does not depend on a
// gateway. It is an underlay VLAN with no gateway leg on it at all: a tenant's
// router gets a port directly, so an egress gateway falling over takes the
// internet with it and leaves the control plane — and the CSI path to the host
// API that rides the same leg — untouched.
package transit

import (
	"fmt"
	"net/netip"
	"regexp"
	"sort"
	"strings"
)

const (
	// GuardPriority is the policy route that keeps transit-bound traffic out
	// of the default route.
	//
	// Policy routes are evaluated before static ones, so an egress gateway's
	// catch-all would otherwise swallow everything — including the packets
	// going to the control plane one hop away on the attached leg. High enough
	// to sit above any catch-all a gateway installs.
	GuardPriority = 30000

	// AllowPriority is one tenant's permission to reach its control plane.
	AllowPriority = 3200

	// DenyPriority is the baseline those allows are exceptions to.
	DenyPriority = 3000
)

// Rule is one entry in a kube-ovn subnet's ACL list.
type Rule struct {
	Action    string `json:"action"`
	Direction string `json:"direction"`
	Priority  int    `json:"priority"`
	Match     string `json:"match"`
}

// Guard is the policy route a VPC needs before it is attached to the plane.
func Guard(transitCIDR string) map[string]any {
	return map[string]any{
		"action":   "allow",
		"priority": int64(GuardPriority),
		"match":    "ip4.dst == " + transitCIDR,
	}
}

// Allows are one tenant's permissions: its own address to its own control
// plane, on its own ports and nothing else.
//
// UDP is listed separately rather than inferred, because the one UDP port is
// load-bearing in a way that is easy to miss. Talos will not start a kubelet
// until the clock is synchronised, so a guard allowing only the TCP ports drops
// the time request and presents as a node that never joins — the same symptom
// as everything else on this path, and the reason it costs an afternoon.
func Allows(eip, vip string, tcp, udp []int) []Rule {
	var rules []Rule
	for _, group := range []struct {
		proto string
		ports []int
	}{{"tcp", tcp}, {"udp", udp}} {
		for _, port := range group.ports {
			if port == 0 {
				continue
			}
			rules = append(rules, Rule{
				Action:    "allow-related",
				Direction: "from-lport",
				Priority:  AllowPriority,
				Match: fmt.Sprintf("ip4.src == %s && ip4.dst == %s && %s.dst == %d",
					eip, vip, group.proto, port),
			})
		}
	}
	return rules
}

// Deny is the baseline every tenant's allow punches through.
//
// Without it the plane is open: allows are additive, so one tenant's address
// could reach another tenant's control-plane ports and the nodes sitting on the
// same subnet.
//
// Scoped by **source** to the range kube-ovn allocates addresses from — the
// subnet minus its excludeIps. Using the whole subnet instead would put the
// nodes' own addresses and the control-plane VIP on the left of a drop rule.
// And the range is taken whole rather than as its first /24: a /24-scoped rule
// is a rule about the tenants numbered lowest, and the 129th is allocated
// outside it, keeps its allow, and quietly falls out of the deny.
func Deny(transitCIDR string, excludeIPs []string) Rule {
	ranges := AllocatableRanges(transitCIDR, excludeIPs)
	if len(ranges) == 0 {
		if prefix, err := netip.ParsePrefix(transitCIDR); err == nil {
			ranges = []string{prefix.Masked().String()}
		} else {
			ranges = []string{transitCIDR}
		}
	}
	match := "ip4.src == " + ranges[0]
	if len(ranges) > 1 {
		match = "ip4.src == {" + strings.Join(ranges, ", ") + "}"
	}
	return Rule{
		Action: "drop", Direction: "from-lport",
		Priority: DenyPriority, Match: match,
	}
}

var sourceOf = regexp.MustCompile(`ip4\.src\s*==\s*([0-9.]+)\b`)

// AllowSource is the address an allow is keyed on, or "" if it names none.
func AllowSource(match string) string {
	if m := sourceOf.FindStringSubmatch(match); m != nil {
		return m[1]
	}
	return ""
}

// AllocatableRanges is the part of a subnet kube-ovn can hand addresses out of.
//
// excludeIps is where the deployment records what is reserved — on this lab the
// transit subnet is a /22 with its first /24 excluded, because that /24 holds
// the nodes and the control-plane VIP. What is left is what tenants get.
func AllocatableRanges(cidr string, excludeIPs []string) []string {
	network, err := netip.ParsePrefix(cidr)
	if err != nil {
		return nil
	}
	network = network.Masked()

	remaining := []netip.Prefix{network}
	for _, entry := range excludeIPs {
		for _, block := range parseReserved(entry) {
			var next []netip.Prefix
			for _, prefix := range remaining {
				next = append(next, subtract(prefix, block)...)
			}
			remaining = next
		}
	}

	sort.Slice(remaining, func(i, j int) bool {
		if remaining[i].Addr() != remaining[j].Addr() {
			return remaining[i].Addr().Less(remaining[j].Addr())
		}
		return remaining[i].Bits() < remaining[j].Bits()
	})

	out := make([]string, 0, len(remaining))
	for _, prefix := range remaining {
		// The subnet's own network address survives the arithmetic as a /32 no
		// host can hold. Dropping it keeps the rule readable — these end up in
		// front of whoever is reading an ACL at three in the morning.
		if prefix.Bits() == 32 && prefix.Addr() == network.Addr() {
			continue
		}
		out = append(out, prefix.String())
	}
	return out
}

// parseReserved reads the three forms excludeIps uses: `a..b`, a CIDR, or a
// bare address. What it cannot read it skips — this feeds a diagnostic-shaped
// rule, and one odd line should not decide the whole plane is unallocatable.
func parseReserved(entry string) []netip.Prefix {
	entry = strings.TrimSpace(entry)
	switch {
	case entry == "":
		return nil
	case strings.Contains(entry, ".."):
		first, last, _ := strings.Cut(entry, "..")
		low, lowErr := netip.ParseAddr(strings.TrimSpace(first))
		high, highErr := netip.ParseAddr(strings.TrimSpace(last))
		if lowErr != nil || highErr != nil || high.Less(low) {
			return nil
		}
		return summarise(low, high)
	case strings.Contains(entry, "/"):
		prefix, err := netip.ParsePrefix(entry)
		if err != nil {
			return nil
		}
		return []netip.Prefix{prefix.Masked()}
	default:
		addr, err := netip.ParseAddr(entry)
		if err != nil {
			return nil
		}
		return []netip.Prefix{netip.PrefixFrom(addr, 32)}
	}
}

// summarise covers an inclusive range with the fewest prefixes, the way
// Python's summarize_address_range does — the form the reference is written in.
func summarise(low, high netip.Addr) []netip.Prefix {
	var out []netip.Prefix
	start, end := toUint(low), toUint(high)
	for start <= end {
		bits := 32
		for bits > 0 {
			mask := uint32(1)<<(32-uint(bits)+1) - 1
			if start&mask != 0 || start+mask > end {
				break
			}
			bits--
		}
		out = append(out, netip.PrefixFrom(fromUint(start), bits))
		size := uint32(1) << (32 - uint(bits))
		if start+size-1 >= end {
			break
		}
		start += size
	}
	return out
}

// subtract removes block from prefix, returning what is left.
func subtract(prefix, block netip.Prefix) []netip.Prefix {
	if !prefix.Overlaps(block) {
		return []netip.Prefix{prefix}
	}
	if block.Bits() <= prefix.Bits() {
		// The block covers the whole prefix.
		return nil
	}
	// Split and recurse on the half that still overlaps.
	left := netip.PrefixFrom(prefix.Addr(), prefix.Bits()+1)
	rightAddr := fromUint(toUint(prefix.Addr()) + uint32(1)<<(32-uint(prefix.Bits()+1)))
	right := netip.PrefixFrom(rightAddr, prefix.Bits()+1)

	var out []netip.Prefix
	for _, half := range []netip.Prefix{left, right} {
		if half.Overlaps(block) {
			out = append(out, subtract(half, block)...)
			continue
		}
		out = append(out, half)
	}
	return out
}

func toUint(addr netip.Addr) uint32 {
	octets := addr.As4()
	return uint32(octets[0])<<24 | uint32(octets[1])<<16 |
		uint32(octets[2])<<8 | uint32(octets[3])
}

func fromUint(value uint32) netip.Addr {
	return netip.AddrFrom4([4]byte{
		byte(value >> 24), byte(value >> 16), byte(value >> 8), byte(value),
	})
}
