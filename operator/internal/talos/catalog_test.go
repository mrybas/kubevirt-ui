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

package talos

import (
	"strings"
	"testing"
)

func builtInCatalog(t *testing.T) []Release {
	t.Helper()
	entries, err := Catalog("")
	if err != nil {
		t.Fatalf("catalog: %v", err)
	}
	if len(entries) == 0 {
		t.Fatal("the built-in catalogue is empty")
	}
	return entries
}

// TestTheWindowIsComparedAsNumbers. "1.9" sorts above "1.31" as text, so a
// string comparison silently accepts a version outside the window — and the
// tenant then fails much later, during a join.
func TestTheWindowIsComparedAsNumbers(t *testing.T) {
	release := Release{Talos: "1.13.8", K8sMin: "1.31", K8sMax: "1.36"}
	for version, want := range map[string]bool{
		"1.31":     true,
		"v1.31.0":  true,
		"1.34.1":   true,
		"1.36.9":   true,
		"1.9":      false, // above 1.31 as text, below it as a number
		"1.30.1":   false,
		"1.37":     false,
		"":         false,
		"nonsense": false,
	} {
		if got := Compatible(release, version); got != want {
			t.Errorf("Kubernetes %q: compatible=%v, want %v", version, got, want)
		}
	}
}

// TestAWideWindowIsNotNarrowedByTextOrder.
//
// This is the case a string comparison actually gets wrong, and the reason the
// numbers matter. With a window of 1.9 to 1.31, "1.28" compares *below* "1.9"
// as text — '2' before '9' — so a purely textual check refuses a version that
// is squarely inside. The narrow window in the built-in catalogue happens to
// give the same answer either way, which is why a test written only against it
// proves nothing.
func TestAWideWindowIsNotNarrowedByTextOrder(t *testing.T) {
	release := Release{Talos: "1.11.0", K8sMin: "1.9", K8sMax: "1.31"}
	for version, want := range map[string]bool{
		"1.9":    true,
		"1.10.4": true,
		"1.28.2": true,
		"1.31":   true,
		"1.8.9":  false,
		"1.32":   false,
	} {
		if got := Compatible(release, version); got != want {
			t.Errorf("Kubernetes %q against %s-%s: compatible=%v, want %v",
				version, release.K8sMin, release.K8sMax, got, want)
		}
	}
}

// TestPatchReleasesDoNotNarrowTheWindow. The window is stated in minors and a
// tenant asks for a patch version.
func TestPatchReleasesDoNotNarrowTheWindow(t *testing.T) {
	release := Release{Talos: "1.13.8", K8sMin: "1.31", K8sMax: "1.36"}
	for _, version := range []string{"1.36.0", "1.36.12", "v1.36.99"} {
		if !Compatible(release, version) {
			t.Errorf("%s was refused, though 1.36 is inside the window", version)
		}
	}
}

// TestWhatIsOfferedIsWhatIsAccepted is the anti-drift check.
//
// The failure this codebase keeps meeting is the wizard offering a pair the
// backend then refuses — or worse, accepts. Both sides read one function, and
// this compares the sets rather than the code.
func TestWhatIsOfferedIsWhatIsAccepted(t *testing.T) {
	entries := builtInCatalog(t)
	pairs := CompatiblePairs(entries)
	if len(pairs) == 0 {
		t.Fatal("no pairs — the scan is broken and would pass on anything")
	}

	for pair := range pairs {
		if Refusal(entries, pair[0], pair[1]+".0") != "" {
			t.Errorf("offered %s/%s and refused it", pair[0], pair[1])
		}
	}
	// And the other direction: a minor just outside the widest window must be
	// refused by every entry.
	for _, entry := range entries {
		_, hiMinor, _ := minorOf(entry.K8sMax)
		outside := entry.K8sMin[:strings.Index(entry.K8sMin, ".")+1]
		beyond := outside + itoa(hiMinor+1)
		if Refusal(entries, entry.Talos, beyond) == "" {
			t.Errorf("%s accepted Kubernetes %s, past its own maximum %s",
				entry.Talos, beyond, entry.K8sMax)
		}
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var digits []byte
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	return string(digits)
}

// TestTheRefusalIsTheOneTheWizardShows. A user shown two different
// explanations of one rule concludes there are two rules.
func TestTheRefusalIsTheOneTheWizardShows(t *testing.T) {
	entries := builtInCatalog(t)

	unknown := Refusal(entries, "9.9.9", "1.33")
	if !strings.Contains(unknown, "is not in this deployment's catalogue") ||
		!strings.Contains(unknown, "Offered:") {
		t.Errorf("unknown version: %q", unknown)
	}

	incompatible := Refusal(entries, "1.13.8", "1.30.1")
	for _, phrase := range []string{
		"does not support Kubernetes 1.30.1",
		"it takes 1.31-1.36",
		"Compatible pairs:",
	} {
		if !strings.Contains(incompatible, phrase) {
			t.Errorf("incompatible pair does not say %q: %q", phrase, incompatible)
		}
	}

	if got := Refusal(entries, "1.13.8", "1.33.1"); got != "" {
		t.Errorf("a compatible pair was refused: %q", got)
	}
}

// TestABadOverrideFallsBackAndSaysSo. Leaving the deployment with no Talos at
// all is worse than ignoring the override — but silently ignoring it means the
// operator who wrote it sees their version simply not appear.
func TestABadOverrideFallsBackAndSaysSo(t *testing.T) {
	entries, err := Catalog("this is not json")
	if err == nil {
		t.Error("a malformed catalogue was accepted in silence")
	}
	if len(entries) == 0 {
		t.Error("the fallback left no versions at all")
	}

	entries, err = Catalog(`[]`)
	if err == nil {
		t.Error("an empty catalogue was accepted")
	}
	if len(entries) == 0 {
		t.Error("the fallback left no versions at all")
	}
}

// TestAnOverrideReplacesTheBuiltIn, and fills in the image URL it can derive.
func TestAnOverrideReplacesTheBuiltIn(t *testing.T) {
	entries, err := Catalog(`[{"talos":"1.14.0","k8s_min":"1.32","k8s_max":"1.37"}]`)
	if err != nil {
		t.Fatalf("catalog: %v", err)
	}
	if len(entries) != 1 || entries[0].Talos != "1.14.0" {
		t.Fatalf("entries = %v", entries)
	}
	if !strings.Contains(entries[0].ImageURL, "v1.14.0/openstack-amd64.raw.xz") {
		t.Errorf("image URL = %q", entries[0].ImageURL)
	}
	// And the built-in is gone: an override is a replacement, not an addition.
	if _, ok := Find(entries, "1.13.8"); ok {
		t.Error("the built-in survived an override")
	}
}

// TestTheImageIsTheOpenstackVariant. CAPK attaches a cloudInitConfigDrive,
// which is an OpenStack config-2 disk; the nocloud variant looks for a `cidata`
// disk, does not find one, and sits in maintenance mode — a worker that boots
// fine and never joins.
func TestTheImageIsTheOpenstackVariant(t *testing.T) {
	url := FactoryImageURL("1.13.8")
	if !strings.Contains(url, "openstack-amd64") {
		t.Errorf("image URL = %q", url)
	}
	if strings.Contains(url, "nocloud") {
		t.Errorf("nocloud image: the worker would boot and never join: %q", url)
	}
	if FactoryImageURL("v1.13.8") != url {
		t.Error("a leading v changes the URL")
	}
}
