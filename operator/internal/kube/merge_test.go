package kube

import "testing"

// TestMergeKeepsWhatTheClusterWroteBack.
//
// Two objects taught this the same lesson. A HelmRelease acquires
// `chart.spec.reconcileStrategy` from Flux's defaulting; a KamajiControlPlane
// acquires `controlPlaneEndpoint`, `kine`, `registry`, `controllerManager` and
// `scheduler` from Kamaji's. Replacing a spec strips whichever of those the
// renderer does not set, the owner writes them back, and the two rewrite each
// other for ever — a resourceVersion that never settles with nothing changing.
func TestMergeKeepsWhatTheClusterWroteBack(t *testing.T) {
	live := map[string]any{
		"replicas": int64(2),
		"network": map[string]any{
			"serviceType": "ClusterIP",
			"certSANs":    []any{"a"},
		},
		"controlPlaneEndpoint": map[string]any{"host": "10.103.184.143", "port": int64(6443)},
		"kine":                 map[string]any{"image": "kine:v1"},
	}
	want := map[string]any{
		"replicas": int64(2),
		"network": map[string]any{
			"serviceType": "ClusterIP",
			"certSANs":    []any{"a", "b"},
		},
	}

	merged := MergeSpec(live, want)
	if _, kept := merged["controlPlaneEndpoint"]; !kept {
		t.Error("it stripped the endpoint the control plane wrote about itself")
	}
	if _, kept := merged["kine"]; !kept {
		t.Error("it stripped a defaulted field")
	}
	network, _ := merged["network"].(map[string]any)
	sans, _ := network["certSANs"].([]any)
	if len(sans) != 2 {
		t.Errorf("certSANs = %v — a list this renders is a statement, and half "+
			"of somebody else's list is not a value anybody chose", sans)
	}
	if network["serviceType"] != "ClusterIP" {
		t.Errorf("network = %v", network)
	}
}
