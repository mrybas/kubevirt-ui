package repocheck

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// `latest` must never point at something that was not released.
//
// The rule used to be a shell function inside the workflow, so the only way to
// exercise it was to push a tag. It is a script now, and this runs it.
//
// A prerelease is the case that did not exist. `2026.10.37-dev.1` publishes real
// images and a real chart under its own version, so the whole product can be
// installed on purpose and stays unreachable by accident: SemVer resolvers
// exclude prereleases from ranges unless asked for by name, so such a chart
// cannot be selected by a `2026.10.*` that means production. What this guards is
// the other half — the mutable tag, which no resolver protects.

func tagsFor(t *testing.T, ref string) map[string]string {
	t.Helper()
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatalf("locating the repository: %v", err)
	}
	script := filepath.Join(root, "hack", "image-tags.sh")
	if _, err := os.Stat(script); err != nil {
		t.Fatalf("the script this guards is not at %s: %v", script, err)
	}

	command := exec.Command("bash", script, ref, "ghcr.io", "mrybas/kubevirt-ui")
	command.Env = append(os.Environ(), "GITHUB_SHA=abcdef1234567890")
	out, err := command.Output()
	if err != nil {
		t.Fatalf("running it for %s: %v", ref, err)
	}
	got := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		key, value, found := strings.Cut(line, "=")
		if found {
			got[key] = value
		}
	}
	return got
}

func TestAReleaseMovesLatest(t *testing.T) {
	got := tagsFor(t, "refs/tags/2026.10.37")
	if got["version"] != "2026.10.37" || got["is_release"] != "true" ||
		got["is_prerelease"] != "false" {
		t.Fatalf("got %v", got)
	}
	if !strings.HasSuffix(got["tags_backend"], "backend:latest") {
		t.Errorf("a release did not move latest: %s", got["tags_backend"])
	}
}

func TestAPrereleasePublishesAndMovesNothing(t *testing.T) {
	got := tagsFor(t, "refs/tags/2026.10.37-dev.1")
	if got["version"] != "2026.10.37-dev.1" {
		t.Errorf("version = %s", got["version"])
	}
	// Published: the chart job keys off this one.
	if got["is_release"] != "true" || got["is_prerelease"] != "true" {
		t.Errorf("got %v", got)
	}
	for _, component := range []string{"backend", "frontend", "operator"} {
		if strings.Contains(got["tags_"+component], "latest") {
			t.Errorf("%s would be pullable as latest from a prerelease: %s",
				component, got["tags_"+component])
		}
	}
}

func TestABranchBuildClaimsNoVersion(t *testing.T) {
	got := tagsFor(t, "refs/heads/operator-dev")
	if got["version"] != "dev-abcdef1" || got["is_release"] != "false" {
		t.Fatalf("got %v", got)
	}
	if strings.Contains(got["tags_backend"], "latest") {
		t.Error("a branch build moved latest")
	}
}

func TestEveryShapeOfPrereleaseIsOne(t *testing.T) {
	// A hyphen is what SemVer calls a prerelease, and the script reads it the
	// same way every resolver downstream will.
	for _, ref := range []string{
		"refs/tags/2026.10.37-rc.1",
		"refs/tags/2026.11.0-dev.4",
		"refs/tags/2027.1.0-alpha",
	} {
		got := tagsFor(t, ref)
		if got["is_prerelease"] != "true" {
			t.Errorf("%s read as a full release", ref)
		}
		if strings.Contains(got["tags_operator"], "latest") {
			t.Errorf("%s would move latest", ref)
		}
	}
}
