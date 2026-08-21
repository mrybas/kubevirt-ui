package addons

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"sigs.k8s.io/yaml"
)

// TestTheReleasesMatchTheProduct.
//
// The acceptance for this slice, written down: an addon enabled through the
// operator must produce the release an addon enabled through the old UI does,
// byte for byte. The table was taken once from the implementation that is still
// the reference — with the catalogue the stand actually carries — and is
// asserted by both.
func TestTheReleasesMatchTheProduct(t *testing.T) {
	path := filepath.Join("..", "..", "..", "test", "parity", "addon-releases.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading the parity table: %v", err)
	}
	var table struct {
		Tenant    string          `json:"tenant"`
		Namespace string          `json:"namespace"`
		Catalog   json.RawMessage `json:"catalog"`
		Requested []struct {
			ID         string            `json:"id"`
			Parameters map[string]string `json:"parameters"`
		} `json:"requested"`
		Releases []struct {
			Metadata struct {
				Name      string            `json:"name"`
				Namespace string            `json:"namespace"`
				Labels    map[string]string `json:"labels"`
			} `json:"metadata"`
			Spec map[string]any `json:"spec"`
		} `json:"releases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("the parity table is not readable: %v", err)
	}
	if len(table.Releases) == 0 {
		t.Fatal("the parity table is empty, so this test proves nothing")
	}

	catalogYAML, err := yaml.JSONToYAML(table.Catalog)
	if err != nil {
		t.Fatalf("the catalogue in the table is not readable: %v", err)
	}
	catalog, err := ParseCatalog(string(catalogYAML))
	if err != nil {
		t.Fatalf("parsing the catalogue: %v", err)
	}

	requested := make([]Request, 0, len(table.Requested))
	for _, item := range table.Requested {
		requested = append(requested, Request{ID: item.ID, Parameters: item.Parameters})
	}

	got := Render(table.Tenant, table.Namespace, catalog, requested)
	if len(got) != len(table.Releases) {
		t.Fatalf("rendered %d releases, want %d", len(got), len(table.Releases))
	}
	for i, want := range table.Releases {
		release := got[i]
		t.Run(want.Metadata.Name, func(t *testing.T) {
			if release.Name != want.Metadata.Name ||
				release.Namespace != want.Metadata.Namespace {
				t.Fatalf("got %s/%s, want %s/%s", release.Namespace, release.Name,
					want.Metadata.Namespace, want.Metadata.Name)
			}
			if !reflect.DeepEqual(release.Labels, want.Metadata.Labels) {
				t.Errorf("labels:\n got %v\nwant %v", release.Labels, want.Metadata.Labels)
			}
			// Compared through JSON so an int and an int64 of the same value
			// are the same value, which is what the API server stores.
			gotSpec, _ := json.Marshal(release.Spec)
			wantSpec, _ := json.Marshal(want.Spec)
			var a, b any
			_ = json.Unmarshal(gotSpec, &a)
			_ = json.Unmarshal(wantSpec, &b)
			if !reflect.DeepEqual(a, b) {
				t.Errorf("spec differs:\n got %s\nwant %s", gotSpec, wantSpec)
			}
		})
	}
}

// TestMergeKeepsWhatFluxWroteBack.
//
// A HelmRelease acquires `chart.spec.reconcileStrategy` from Flux's own
// defaulting. Replacing the spec strips it, Flux writes it back, and the two
// rewrite each other for ever — with resourceVersion climbing and nothing
// changing. Found by predicting an adoption against a live tenant rather than
// by running one.
func TestMergeKeepsWhatFluxWroteBack(t *testing.T) {
	live := map[string]any{
		"interval": "30m",
		"chart": map[string]any{"spec": map[string]any{
			"chart":             "./tenant-charts/core/namespaces",
			"interval":          "12h",
			"reconcileStrategy": "ChartVersion",
			"sourceRef":         map[string]any{"kind": "GitRepository", "name": "flux-system"},
		}},
		"suspend": false,
	}
	want := map[string]any{
		"interval": "30m",
		"chart": map[string]any{"spec": map[string]any{
			"chart":     "./tenant-charts/core/namespaces",
			"interval":  "12h",
			"sourceRef": map[string]any{"kind": "GitRepository", "name": "flux-system"},
		}},
	}

	merged := MergeSpec(live, want)
	chart, _ := merged["chart"].(map[string]any)
	spec, _ := chart["spec"].(map[string]any)
	if spec["reconcileStrategy"] != "ChartVersion" {
		t.Errorf("it stripped what Flux wrote back: %v", spec)
	}
	if merged["suspend"] != false {
		t.Errorf("it dropped a field it does not render: %v", merged["suspend"])
	}

	// And what it does render still wins.
	want["interval"] = "1h"
	if merged := MergeSpec(live, want); merged["interval"] != "1h" {
		t.Errorf("interval = %v — the declaration has to win over what is there",
			merged["interval"])
	}
}
