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

// TestTheHandoverFollowsTheOperator.
//
// The chart can install the operator and hand it nothing: the flags that decide
// who writes default to empty, empty means the product keeps writing, and the
// symptom is three healthy controllers doing nothing at all. Nothing is broken,
// nothing is logged. That shape — the change that did not reach the thing it was
// for — is most of this migration's failures, so the four paths that have been
// handed over follow `operator.enabled`.
//
// The other five stay off: they name paths the product still owns.
func TestTheHandoverFollowsTheOperator(t *testing.T) {
	handed := []string{
		"OPERATOR_UNDERLAY_ENABLED",
		"OPERATOR_TENANT_BOOTSTRAP_ENABLED",
		"OPERATOR_TENANT_TIME_ENABLED",
		"OPERATOR_TENANT_ADDONS_ENABLED",
	}
	notYet := []string{
		"OPERATOR_IMAGE_ENABLED", "OPERATOR_VM_ENABLED",
		"OPERATOR_TEMPLATE_ENABLED", "OPERATOR_ANNOUNCE_ENABLED",
		"OPERATOR_NETWORK_ENABLED",
	}

	off := backendEnv(t, false, nil)
	for _, name := range handed {
		if off[name] == "true" {
			t.Errorf("%s is on with no operator to hand to", name)
		}
	}

	on := backendEnv(t, true, nil)
	for _, name := range handed {
		if on[name] != "true" {
			t.Errorf("%s = %q with the operator installed", name, on[name])
		}
	}
	for _, name := range notYet {
		if on[name] == "true" {
			t.Errorf("%s was turned on, and that path is not retired", name)
		}
	}

	// A decision beats a default, including a decision to keep something.
	kept := backendEnv(t, true,
		map[string]string{"OPERATOR_TENANT_TIME_ENABLED": "false"})
	if kept["OPERATOR_TENANT_TIME_ENABLED"] != "false" {
		t.Errorf("an explicit false was overruled: %q",
			kept["OPERATOR_TENANT_TIME_ENABLED"])
	}
	if kept["OPERATOR_TENANT_ADDONS_ENABLED"] != "true" {
		t.Errorf("one override changed the others: %v", kept)
	}
	// And it appears once, not twice with the loop below writing it again.
	if got := strings.Count(rendered, "OPERATOR_TENANT_TIME_ENABLED"); got != 1 {
		t.Errorf("the variable is written %d times, want once — the override "+
			"loop and the handover loop are both emitting it", got)
	}
}

var rendered string

// backendEnv renders the chart and reads the backend's environment out of it.
func backendEnv(t *testing.T, operator bool, overrides map[string]string) map[string]string {
	t.Helper()
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatalf("locating the repository: %v", err)
	}
	if _, err := exec.LookPath("helm"); err != nil {
		// Named rather than skipped: a guard that cannot run is not a guard,
		// and the whole reason this package exists is that a check which
		// quietly does nothing reads exactly like a check that passed.
		t.Fatalf("helm is not installed in this environment, so this guard "+
			"cannot run: %v", err)
	}
	args := []string{"template", "kubevirt-ui",
		filepath.Join(root, "helm", "kubevirt-ui")}
	if operator {
		args = append(args,
			"--set", "operator.enabled=true",
			"--set", "operator.config.kubeOvnNamespace=k",
			"--set", "operator.config.metallbNamespace=m",
			"--set", "operator.config.metallbPool=p",
			"--set", "operator.config.cpTransitSubnet=c",
			"--set", "operator.config.ingressDomain=d")
	}
	for key, value := range overrides {
		args = append(args, "--set", "backend.env."+key+"="+value)
	}
	out, err := exec.Command("helm", args...).Output()
	if err != nil {
		t.Fatalf("rendering the chart: %v", err)
	}
	rendered = string(out)

	// Read the backend container's env without a YAML library: the block is
	// `- name: X` / `value: "Y"` pairs, and pulling in a parser for four
	// variables would be more machinery than the thing it inspects.
	env := map[string]string{}
	lines := strings.Split(rendered, "\n")
	for i, line := range lines {
		name := strings.TrimPrefix(strings.TrimSpace(line), "- name: ")
		if !strings.HasPrefix(name, "OPERATOR_") || i+1 >= len(lines) {
			continue
		}
		value := strings.TrimPrefix(strings.TrimSpace(lines[i+1]), "value: ")
		env[name] = strings.Trim(value, `"`)
	}
	return env
}
