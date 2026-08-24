package tenant

import "testing"

func TestParseRangesReadsEveryFormTheseObjectsUse(t *testing.T) {
	got := ParseRanges([]string{
		"10.199.0.100-10.199.0.120", "10.199.1.0/30", "10.199.2.5", "", "nonsense",
	}, "-")
	want := []string{
		"10.199.0.100-10.199.0.120",
		"10.199.1.0-10.199.1.3",
		"10.199.2.5-10.199.2.5",
	}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range got {
		if got[i].String() != want[i] {
			t.Errorf("range %d = %s, want %s", i, got[i], want[i])
		}
	}
}

// TestAdjacentExclusionsCountAsOne is why Covered unions rather than compares.
// excludeIps are written as several entries as often as one, and a pool that
// spans two of them is excluded just as completely.
func TestAdjacentExclusionsCountAsOne(t *testing.T) {
	uncovered := Uncovered(
		[]string{"10.199.0.100-10.199.0.200"},
		[]string{"10.199.0.100..10.199.0.150", "10.199.0.151..10.199.0.255"},
	)
	if len(uncovered) != 0 {
		t.Errorf("a fully excluded pool reported as uncovered: %v", uncovered)
	}
}

func TestAPoolPokingOutOfTheExclusionIsNamed(t *testing.T) {
	uncovered := Uncovered(
		[]string{"10.199.0.100-10.199.0.200", "10.199.5.0-10.199.5.10"},
		[]string{"10.199.0.100..10.199.0.255"},
	)
	if len(uncovered) != 1 {
		t.Fatalf("uncovered = %v, want the second range only", uncovered)
	}
	if uncovered[0].String() != "10.199.5.0-10.199.5.10" {
		t.Errorf("uncovered = %s", uncovered[0])
	}
}

// TestPartialCoverIsNotCover. Half an exclusion is the dangerous case: the
// addresses at the top of the pool are the ones both allocators can hand out.
func TestPartialCoverIsNotCover(t *testing.T) {
	uncovered := Uncovered(
		[]string{"10.199.0.100-10.199.0.200"},
		[]string{"10.199.0.100..10.199.0.150"},
	)
	if len(uncovered) != 1 {
		t.Fatalf("a half-excluded pool reported as safe: %v", uncovered)
	}
}

func TestNoExclusionsAtAllIsReported(t *testing.T) {
	if got := Uncovered([]string{"10.199.0.100-10.199.0.200"}, nil); len(got) != 1 {
		t.Errorf("a subnet excluding nothing reported as covering: %v", got)
	}
}
