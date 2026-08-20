package tenant

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// TestTheReservationAgreesWithTheProduct reads one table that both
// implementations assert against.
//
// Neither side generates it. While the backend still computes tenant quotas —
// and it does, for every tenant the product creates — two arithmetics decide
// the same number, and the interesting failure is not either being wrong but
// the two disagreeing: a tenant adopted from one and reconciled by the other
// would have its quota rewritten on the first pass, silently, in a direction
// nobody chose.
//
// The file is in Gi for legibility and exact bytes for the expectation. Moving
// either arithmetic turns its own suite red against this file, which is where
// the disagreement should surface — before a cluster.
func TestTheReservationAgreesWithTheProduct(t *testing.T) {
	path := filepath.Join("..", "..", "..", "test", "parity", "tenant-quota.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading the parity table: %v", err)
	}
	var table struct {
		Cases []struct {
			Name                 string `json:"name"`
			Workers              int    `json:"workers"`
			VCPU                 int    `json:"vcpu"`
			MemoryGi             int    `json:"memoryGi"`
			DiskGi               int    `json:"diskGi"`
			ControlPlaneReplicas int    `json:"controlPlaneReplicas"`
			Talos                bool   `json:"talos"`
			Expected             struct {
				CPUMilli     int64 `json:"cpuMilli"`
				MemoryBytes  int64 `json:"memoryBytes"`
				StorageBytes int64 `json:"storageBytes"`
			} `json:"expected"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("the parity table is not readable: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("the parity table is empty, so this test proves nothing")
	}

	for _, tc := range table.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			got, err := Reserve(Sizing{
				Workers: tc.Workers, VCPU: tc.VCPU,
				Memory:       fmt.Sprintf("%dGi", tc.MemoryGi),
				Disk:         fmt.Sprintf("%dGi", tc.DiskGi),
				CPReplicas:   tc.ControlPlaneReplicas,
				TalosWorkers: tc.Talos,
			})
			if err != nil {
				t.Fatalf("reserve: %v", err)
			}
			if got.CPU.MilliValue() != tc.Expected.CPUMilli {
				t.Errorf("cpu = %dm, want %dm", got.CPU.MilliValue(), tc.Expected.CPUMilli)
			}
			if got.Memory.Value() != tc.Expected.MemoryBytes {
				t.Errorf("memory = %d, want %d", got.Memory.Value(), tc.Expected.MemoryBytes)
			}
			if got.Storage.Value() != tc.Expected.StorageBytes {
				t.Errorf("storage = %d, want %d", got.Storage.Value(), tc.Expected.StorageBytes)
			}
		})
	}
}
