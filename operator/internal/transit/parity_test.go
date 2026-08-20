package transit

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// TestTheRulesMatchTheProduct.
//
// The table was taken once from the implementation that is still the reference
// and is now asserted by both. Byte for byte on purpose: an allow is keyed on an
// address and the deny is what it punches through, so a difference of one prefix
// is a tenant that cannot reach its control plane — or one that can reach
// somebody else's.
func TestTheRulesMatchTheProduct(t *testing.T) {
	path := filepath.Join("..", "..", "..", "test", "parity", "transit-rules.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading the parity table: %v", err)
	}
	var table struct {
		AllowsExample struct {
			EIP   string `json:"eip"`
			VIP   string `json:"vip"`
			TCP   []int  `json:"tcp"`
			UDP   []int  `json:"udp"`
			Rules []Rule `json:"rules"`
		} `json:"allowsExample"`
		Cases []struct {
			Name       string   `json:"name"`
			CIDR       string   `json:"cidr"`
			ExcludeIPs []string `json:"excludeIps"`
			Ranges     []string `json:"ranges"`
			Deny       Rule     `json:"deny"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("the parity table is not readable: %v", err)
	}
	if len(table.Cases) == 0 || len(table.AllowsExample.Rules) == 0 {
		t.Fatal("the parity table is empty, so this test proves nothing")
	}

	example := table.AllowsExample
	if got := Allows(example.EIP, example.VIP, example.TCP, example.UDP); !reflect.DeepEqual(got, example.Rules) {
		t.Errorf("allows:\n got %+v\nwant %+v", got, example.Rules)
	}

	for _, tc := range table.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			got := AllocatableRanges(tc.CIDR, tc.ExcludeIPs)
			if len(got) == 0 && len(tc.Ranges) == 0 {
				return
			}
			if !reflect.DeepEqual(got, tc.Ranges) {
				t.Errorf("ranges:\n got %v\nwant %v", got, tc.Ranges)
			}
			if deny := Deny(tc.CIDR, tc.ExcludeIPs); deny != tc.Deny {
				t.Errorf("deny:\n got %+v\nwant %+v", deny, tc.Deny)
			}
		})
	}
}

// TestTheGuardSitsAboveAnyCatchAll. Policy routes are evaluated before static
// ones, so a gateway's catch-all would swallow the packets going to a control
// plane one hop away on the attached leg.
func TestTheGuardSitsAboveAnyCatchAll(t *testing.T) {
	guard := Guard("10.199.0.0/22")
	if guard["priority"].(int64) <= 29100 {
		t.Errorf("priority %v is at or below the egress gateway's catch-all",
			guard["priority"])
	}
	if guard["match"] != "ip4.dst == 10.199.0.0/22" {
		t.Errorf("match = %v", guard["match"])
	}
}

func TestTheAllowSourceIsTheAddressItIsKeyedOn(t *testing.T) {
	match := "ip4.src == 10.199.1.4 && ip4.dst == 10.199.0.101 && tcp.dst == 6443"
	if got := AllowSource(match); got != "10.199.1.4" {
		t.Errorf("source = %q", got)
	}
	if got := AllowSource("ip4.dst == 10.199.0.0/22"); got != "" {
		t.Errorf("a rule with no source reported %q", got)
	}
}
