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

package naming

import "testing"

// The cases are the ones the backend's own docstring pins down. If the two
// implementations drift, objects created through the operator stop matching the
// selectors the UI filters with — which looks like "the image vanished", not
// like a slug bug.
func TestSlugMatchesTheBackendExamples(t *testing.T) {
	cases := map[string]string{
		"Ubuntu 24.04 Server": "ubuntu-24-04-server",
		"My   Weird---Name!!!": "my-weird-name",
		"":                     "unnamed",
		"!!!":                  "unnamed",
		"123abc":               "123abc",
	}
	for in, want := range cases {
		if got := Slug(in); got != want {
			t.Errorf("Slug(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSlugFitsAGenerateNameSeed(t *testing.T) {
	long := ""
	for range 200 {
		long += "a"
	}
	got := Slug(long)
	if len(got) != slugMax {
		t.Fatalf("slug length = %d, want %d", len(got), slugMax)
	}
}

func TestSlugNeverEndsInADash(t *testing.T) {
	// A truncation that lands on a separator would produce "name-" and then
	// "name--x7k2p" once the API server appends its suffix: not a DNS-1123 name.
	in := ""
	for range slugMax - 1 {
		in += "a"
	}
	in += " tail"
	got := Slug(in)
	if got[len(got)-1] == '-' {
		t.Fatalf("slug %q ends in a dash", got)
	}
}
