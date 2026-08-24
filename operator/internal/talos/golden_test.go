package talos

import "testing"

func TestGoldenNameIsTheCatalogueKey(t *testing.T) {
	for _, tc := range []struct{ version, want string }{
		{"1.13.8", "talos-golden-1-13-8"},
		{"v1.13.8", "talos-golden-1-13-8"},
		{" 1.9.0 ", "talos-golden-1-9-0"},
	} {
		if got := GoldenName(tc.version); got != tc.want {
			t.Errorf("GoldenName(%q) = %q, want %q", tc.version, got, tc.want)
		}
	}
}

// TestTwoTenantsOfOneVersionNameOneImage is the property the sharing rests on,
// stated as a test so nobody "improves" the name with anything per-tenant.
func TestTwoTenantsOfOneVersionNameOneImage(t *testing.T) {
	if GoldenName("1.13.8") != GoldenName("v1.13.8") {
		t.Fatal("the same release resolved to two images")
	}
	if GoldenName("1.13.8") == GoldenName("1.14.0") {
		t.Fatal("two releases share one image, so an upgrade would silently " +
			"reuse the old disk")
	}
}

func TestTheGoldenOfTheBuiltInReleaseHasSomethingToImport(t *testing.T) {
	entries, err := Catalog("")
	if err != nil {
		t.Fatalf("catalogue: %v", err)
	}
	entry, ok := Find(entries, "v1.13.8")
	if !ok {
		t.Fatal("the built-in release is not in its own catalogue")
	}
	if entry.ImageURL == "" {
		t.Error("the entry carries no image to import, so its golden would be " +
			"a name with nothing behind it")
	}
}
