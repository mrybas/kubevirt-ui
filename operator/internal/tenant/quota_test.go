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

import "testing"

// TestTheReservationMatchesTheProduct.
//
// The expected numbers are not derived here — they are what the running
// backend returns for the same requests, read off it rather than recomputed.
// A port that agrees with my reading of the formula and disagrees with the
// formula would pass a test written the other way.
func TestTheReservationMatchesTheProduct(t *testing.T) {
	for _, tc := range []struct {
		name                      string
		sizing                    Sizing
		cpuMilli, memory, storage int64
	}{
		{
			name:   "the defaults",
			sizing: Sizing{Workers: 2, VCPU: 2, Memory: "2Gi", Disk: "20Gi", CPReplicas: 2},
			// cpu "7", memory 9651617792, storage 64424509440
			cpuMilli: 7000, memory: 9651617792, storage: 64424509440,
		},
		{
			name: "a larger cloud-init tenant",
			sizing: Sizing{
				Workers: 4, VCPU: 4, Memory: "8Gi", Disk: "40Gi", CPReplicas: 3,
			},
			cpuMilli: 21500, memory: 48123871232, storage: 214748364800,
		},
		{
			name: "talos, disk under the golden floor",
			sizing: Sizing{
				Workers: 2, VCPU: 2, Memory: "2Gi", Disk: "10Gi", CPReplicas: 2,
				TalosWorkers: true,
			},
			cpuMilli: 7000, memory: 9651617792, storage: 96636764160,
		},
		{
			name: "talos, disk above it",
			sizing: Sizing{
				Workers: 1, VCPU: 8, Memory: "32Gi", Disk: "60Gi", CPReplicas: 1,
				TalosWorkers: true,
			},
			cpuMilli: 16500, memory: 70784122880, storage: 257698037760,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Reserve(tc.sizing)
			if err != nil {
				t.Fatalf("reserve: %v", err)
			}
			if got.CPU.MilliValue() != tc.cpuMilli {
				t.Errorf("cpu = %dm, want %dm", got.CPU.MilliValue(), tc.cpuMilli)
			}
			if got.Memory.Value() != tc.memory {
				t.Errorf("memory = %d, want %d", got.Memory.Value(), tc.memory)
			}
			if got.Storage.Value() != tc.storage {
				t.Errorf("storage = %d, want %d", got.Storage.Value(), tc.storage)
			}
		})
	}
}

// TestTheOverheadMatchesWhatThePodsAsked. Sizing a slot at the declared memory
// is what deadlocked a worker rollout: quota 8Gi, two workers and the control
// plane using 5.79Gi, the replacement pod asking 2.273Gi against 2.21Gi free.
// Short by 0.06Gi, forever, because a rollout that cannot start never finishes.
func TestTheOverheadMatchesWhatThePodsAsked(t *testing.T) {
	for _, tc := range []struct {
		memory int64
		vcpu   int
		want   int64
	}{
		{2 << 30, 2, 353894400},
		{8 << 30, 4, 390594560},
		{32 << 30, 8, 495452160},
	} {
		if got := VMIMemoryOverhead(tc.memory, tc.vcpu); got != tc.want {
			t.Errorf("%d bytes / %d vCPU: overhead %d, want %d",
				tc.memory, tc.vcpu, got, tc.want)
		}
	}
}

// TestTheOverheadScalesWithMemory. Page tables are what make this a formula
// rather than a constant: a reserve chosen from a 2Gi worker starves a 256Gi
// one, and only the largest tenants — the ones least likely to be tested —
// would ever hit it.
func TestTheOverheadScalesWithMemory(t *testing.T) {
	small := VMIMemoryOverhead(2<<30, 2)
	large := VMIMemoryOverhead(256<<30, 2)
	if large <= small {
		t.Fatalf("a 256Gi guest reserves %d, no more than a 2Gi one at %d",
			large, small)
	}
}

// TestTheSurgeCoversAReplacement. Sized to exactly the worker count, the quota
// refused every replacement: remediation creates the new Machine while the old
// one is still there.
func TestTheSurgeCoversAReplacement(t *testing.T) {
	two, err := Reserve(Sizing{Workers: 2, VCPU: 2, Memory: "2Gi", Disk: "20Gi", CPReplicas: 1})
	if err != nil {
		t.Fatalf("reserve: %v", err)
	}
	three, err := Reserve(Sizing{Workers: 3, VCPU: 2, Memory: "2Gi", Disk: "20Gi", CPReplicas: 1})
	if err != nil {
		t.Fatalf("reserve: %v", err)
	}
	// Two workers reserve three slots, so a two-worker tenant reserves exactly
	// what a three-worker one needs at steady state.
	if two.Storage.Value() != 3*int64(20<<30) {
		t.Errorf("two workers reserve %d, not three slots", two.Storage.Value())
	}
	if three.Storage.Value() != 4*int64(20<<30) {
		t.Errorf("three workers reserve %d, not four slots", three.Storage.Value())
	}
}

// TestTheGoldenFloorIsNotUndercut: a Talos worker's root is a clone of the
// golden image, so it cannot be smaller than it however small the request.
func TestTheGoldenFloorIsNotUndercut(t *testing.T) {
	small, err := Reserve(Sizing{
		Workers: 1, VCPU: 1, Memory: "1Gi", Disk: "5Gi", CPReplicas: 1,
		TalosWorkers: true,
	})
	if err != nil {
		t.Fatalf("reserve: %v", err)
	}
	// Two slots of the 5Gi disk, plus two slots of the 20Gi floor.
	want := int64(2*(5<<30) + 2*(20<<30))
	if small.Storage.Value() != want {
		t.Errorf("storage = %d, want %d", small.Storage.Value(), want)
	}
}

// TestTheGoldenImageIsNotChargedToTheTenant. It lives in a shared namespace;
// reserving it here would be a quota that does not describe the namespace it
// governs.
func TestTheGoldenImageIsNotChargedToTheTenant(t *testing.T) {
	cloudInit, _ := Reserve(Sizing{
		Workers: 2, VCPU: 2, Memory: "2Gi", Disk: "20Gi", CPReplicas: 1})
	talos, _ := Reserve(Sizing{
		Workers: 2, VCPU: 2, Memory: "2Gi", Disk: "20Gi", CPReplicas: 1,
		TalosWorkers: true})

	// Exactly the root clones — three slots — and not one image more.
	extra := talos.Storage.Value() - cloudInit.Storage.Value()
	if extra != 3*int64(20<<30) {
		t.Errorf("talos adds %d, want exactly the three root clones (%d)",
			extra, 3*int64(20<<30))
	}
}
