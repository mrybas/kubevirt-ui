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

package tenant

import (
	"net/netip"
	"sort"
	"strings"
)

// Range is an inclusive span of IPv4 addresses as integers.
type Range struct {
	Low, High uint32
}

// String renders a range the way the objects it came from write it.
func (r Range) String() string {
	return addr(r.Low).String() + "-" + addr(r.High).String()
}

func addr(value uint32) netip.Addr {
	return netip.AddrFrom4([4]byte{
		byte(value >> 24), byte(value >> 16), byte(value >> 8), byte(value),
	})
}

func toUint(text string) (uint32, bool) {
	parsed, err := netip.ParseAddr(strings.TrimSpace(text))
	if err != nil || !parsed.Is4() {
		return 0, false
	}
	octets := parsed.As4()
	return uint32(octets[0])<<24 | uint32(octets[1])<<16 |
		uint32(octets[2])<<8 | uint32(octets[3]), true
}

// ParseRanges reads the three forms these objects use: `a-b` or `a..b`
// depending on who wrote them, a CIDR, or a bare address.
//
// An entry it cannot read is skipped rather than fatal. The caller is a
// diagnostic, and a diagnostic that refuses to run because one line is odd
// stops being one.
func ParseRanges(entries []string, separator string) []Range {
	var out []Range
	for _, raw := range entries {
		text := strings.TrimSpace(raw)
		switch {
		case text == "":
			continue
		case strings.Contains(text, separator):
			parts := strings.SplitN(text, separator, 2)
			low, lowOK := toUint(parts[0])
			high, highOK := toUint(parts[1])
			if lowOK && highOK && low <= high {
				out = append(out, Range{low, high})
			}
		case strings.Contains(text, "/"):
			prefix, err := netip.ParsePrefix(text)
			if err != nil || !prefix.Addr().Is4() {
				continue
			}
			prefix = prefix.Masked()
			low, ok := toUint(prefix.Addr().String())
			if !ok {
				continue
			}
			size := uint32(1)<<(32-prefix.Bits()) - 1
			out = append(out, Range{low, low + size})
		default:
			if one, ok := toUint(text); ok {
				out = append(out, Range{one, one})
			}
		}
	}
	return out
}

// Covered says whether inner lies entirely inside the union of outer.
//
// The union matters: excludeIps are written as several adjacent entries as
// often as one, and treating them separately would report a perfectly excluded
// pool as uncovered.
func Covered(inner Range, outer []Range) bool {
	sorted := append([]Range(nil), outer...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Low < sorted[j].Low })

	low := inner.Low
	for _, span := range sorted {
		if span.Low > low {
			break
		}
		if span.High >= inner.High {
			return true
		}
		if span.High >= low {
			low = span.High + 1
		}
	}
	return low > inner.High
}

// Uncovered is the part of a MetalLB pool that a subnet does not exclude.
//
// kube-ovn allocates router legs and EIPs from the same subnet, so a range
// MetalLB may hand out and kube-ovn may hand out is a duplicate address waiting
// to happen — discovered, when it happens, as an outage rather than a conflict.
func Uncovered(poolAddresses, excludeIps []string) []Range {
	excluded := ParseRanges(excludeIps, "..")
	var out []Range
	for _, span := range ParseRanges(poolAddresses, "-") {
		if !Covered(span, excluded) {
			out = append(out, span)
		}
	}
	return out
}
