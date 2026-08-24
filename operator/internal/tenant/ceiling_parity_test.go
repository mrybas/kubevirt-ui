package tenant

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/api/resource"
)

// TestTheCeilingAgreesWithTheProduct reads the table the backend asserts too.
//
// The operator writes a tenant's quota from its description without going
// through the API that used to be the only thing asking the folder whether it
// fits. Now both ask, which is only an improvement while both answer the same:
// a tenant refused by the API and admitted by its reconciler is worse than
// either answer alone, because the CR and the cluster then disagree about
// something neither will report.
func TestTheCeilingAgreesWithTheProduct(t *testing.T) {
	path := filepath.Join("..", "..", "..", "test", "parity", "folder-ceiling.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading the parity table: %v", err)
	}
	var table struct {
		Cases []struct {
			Name    string `json:"name"`
			Folders map[string]struct {
				Parent *string           `json:"parent"`
				Quota  map[string]string `json:"quota"`
			} `json:"folders"`
			Namespaces []NamespaceQuota `json:"namespaces"`
			Folder     string           `json:"folder"`
			Asking     string           `json:"asking"`
			Want       struct {
				CPU     string `json:"cpu"`
				Memory  string `json:"memory"`
				Storage string `json:"storage"`
			} `json:"want"`
			Expected struct {
				Refused   bool   `json:"refused"`
				Dimension string `json:"dimension"`
			} `json:"expected"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parsing the parity table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("the parity table is empty, so this test proves nothing")
	}

	for _, c := range table.Cases {
		t.Run(c.Name, func(t *testing.T) {
			tree := map[string]FolderNode{}
			for name, f := range c.Folders {
				parent := ""
				if f.Parent != nil {
					parent = *f.Parent
				}
				tree[name] = FolderNode{Parent: parent, Ceiling: Ceiling{
					CPU:     f.Quota["cpu"],
					Memory:  f.Quota["memory"],
					Storage: f.Quota["storage"],
				}}
			}
			want := Quota{
				CPU:     resource.MustParse(c.Want.CPU),
				Memory:  resource.MustParse(c.Want.Memory),
				Storage: resource.MustParse(c.Want.Storage),
			}

			err := CheckCeiling(tree, c.Namespaces, c.Folder, c.Asking, want)
			if c.Expected.Refused {
				if err == nil {
					t.Fatalf("expected a refusal on %s, got room", c.Expected.Dimension)
				}
				refusal, ok := err.(*CeilingRefusal)
				if !ok {
					t.Fatalf("expected a CeilingRefusal, got %T", err)
				}
				if refusal.Dimension != c.Expected.Dimension {
					t.Fatalf("refused %s, the table says %s: %v",
						refusal.Dimension, c.Expected.Dimension, err)
				}
				if !strings.Contains(err.Error(), "is free and") {
					t.Fatalf("a refusal has to say what is free: %q", err.Error())
				}
				return
			}
			if err != nil {
				t.Fatalf("expected room, got: %v", err)
			}
		})
	}
}
