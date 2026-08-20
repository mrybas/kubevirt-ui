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
	"net/netip"
	"strings"
)

// Verdict is what a rule set does to one packet.
type Verdict string

const (
	// Allowed covers both an explicit allow and no rule matching at all —
	// which is the same outcome, and the reason the internet keeps working
	// when the isolation floor is scoped to the tenant supernet.
	Allowed Verdict = "allow"
	Dropped Verdict = "drop"
	// Conflicted is an allow and a drop matching at the same priority.
	//
	// Not a verdict OVN gives: it picks one, and which one is not specified. So
	// this is reported rather than resolved. A rule set that produces it does
	// not have a defined meaning, and a composer that emits one has a bug that
	// would otherwise show up as traffic that works on some nodes.
	Conflicted Verdict = "conflict"
)

// Evaluate answers what a rule set does to traffic from `source` arriving at
// this subnet, the way OVN does it: highest priority wins, no match means
// allowed.
//
// This exists so two rule sets can be compared by what they *do* rather than by
// what they look like. Replacing five enumerated drops with one aggregate drop
// changes every line of the list and must change no outcome, and only an
// evaluation can say that.
func Evaluate(rules []Rule, source netip.Addr, direction string) Verdict {
	best := -1
	allowed, dropped := false, false
	for _, rule := range rules {
		if rule.Direction != direction {
			continue
		}
		prefix, ok := matchPrefix(rule.Match)
		if !ok || !prefix.Contains(source) {
			continue
		}
		if rule.Priority < best {
			continue
		}
		if rule.Priority > best {
			best, allowed, dropped = rule.Priority, false, false
		}
		if rule.Action == "drop" {
			dropped = true
		} else {
			allowed = true
		}
	}
	switch {
	case allowed && dropped:
		return Conflicted
	case dropped:
		return Dropped
	default:
		// No rule matched, or an allow did. Both mean the packet goes through,
		// and the first is why the internet keeps working when the isolation
		// floor is scoped to the tenant aggregate.
		return Allowed
	}
}

// matchPrefix understands the one match shape this product writes:
// `ip4.src == <cidr>` and `ip4.dst == <cidr>`.
//
// Anything else returns false rather than being guessed at — a match expression
// nobody here wrote is exactly the kind of rule adoption must decline, not
// reinterpret.
func matchPrefix(match string) (netip.Prefix, bool) {
	parts := strings.SplitN(match, "==", 2)
	if len(parts) != 2 {
		return netip.Prefix{}, false
	}
	field := strings.TrimSpace(parts[0])
	if field != "ip4.src" && field != "ip4.dst" {
		return netip.Prefix{}, false
	}
	prefix, err := netip.ParsePrefix(strings.TrimSpace(parts[1]))
	if err != nil {
		return netip.Prefix{}, false
	}
	return prefix, true
}
