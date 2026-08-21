package chartsync_test

import (
	"path/filepath"
	"testing"

	"github.com/mrybas/kubevirt-ui/operator/internal/chartsync"
)

// TestTheChartCarriesTodaysOperator.
//
// The chart's CRDs and the manager ClusterRole are generated from the markers
// in this module. This is the guard that the copies in the chart are today's.
//
// It lives here rather than beside the product's other contract tests for a
// dull and decisive reason: the backend's test container mounts only
// `backend/`, so a check written there cannot see the chart at all. It was
// written there first and passed — vacuously, the same disease as an assertion
// inside `if patched:`.
func TestTheChartCarriesTodaysOperator(t *testing.T) {
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatalf("locating the repository: %v", err)
	}
	drifted, err := chartsync.Drifted(root, true)
	if err != nil {
		t.Fatalf("rendering the chart's copies: %v", err)
	}
	if len(drifted) > 0 {
		t.Fatalf("the chart still has the old %v — run "+
			"`go run ./cmd/chartsync` from operator/", drifted)
	}
}
