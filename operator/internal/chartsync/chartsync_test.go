package chartsync_test

import (
	"path/filepath"
	"strings"
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

// TestEveryCRDIsKeptWhenTheReleaseGoes.
//
// The CRDs are templates rather than files in `crds/`, and that choice hangs on
// this annotation. Helm never *upgrades* what is in `crds/` — the schema would
// silently stay at whatever version was installed first, and the alternative
// offered was "remember to kubectl apply them on every release", a manual step
// whose omission is invisible until a field the API server prunes goes missing
// from an object nobody is looking at.
//
// The price of templating them is that uninstall could delete them, taking
// every tenant, network and VM with it. Measured on the stand rather than
// assumed: a throwaway chart with one annotated CRD and one custom object of
// that kind, installed and then uninstalled — both survived. So the annotation
// is what makes the choice safe, and this is the guard that it stays on all of
// them.
func TestEveryCRDIsKeptWhenTheReleaseGoes(t *testing.T) {
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatalf("locating the repository: %v", err)
	}
	files, err := chartsync.Files(root)
	if err != nil {
		t.Fatalf("rendering: %v", err)
	}
	crds := files["helm/kubevirt-ui/templates/operator-crds.yaml"]

	definitions := strings.Count(crds, "kind: CustomResourceDefinition")
	kept := strings.Count(crds, "helm.sh/resource-policy: keep")
	if definitions == 0 {
		t.Fatal("no CRDs rendered at all")
	}
	if kept != definitions {
		t.Errorf("%d CRDs, %d carry the keep annotation — uninstalling the UI "+
			"would cascade-delete what the others describe", definitions, kept)
	}
}
