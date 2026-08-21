package repocheck

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"
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
		// The tenant flags move together. Three of them write *parts* of a
		// tenant, and handing those over without the tenant itself gives them
		// to a controller that has never heard of it: measured on the stand,
		// `tenant-test2` came up with no CNI, the backend logging "missing
		// required addons" every thirty seconds and deliberately doing nothing
		// because the flag said the addons were not its job, the operator idle
		// because nothing described the tenant, both workers NodeHealthy=False
		// for ever.
		"OPERATOR_TENANT_ENABLED",
		"OPERATOR_TENANT_BOOTSTRAP_ENABLED",
		"OPERATOR_TENANT_TIME_ENABLED",
		"OPERATOR_TENANT_ADDONS_ENABLED",
	}
	notYet := []string{
		"OPERATOR_IMAGE_ENABLED", "OPERATOR_VM_ENABLED",
		"OPERATOR_TEMPLATE_ENABLED", "OPERATOR_ANNOUNCE_ENABLED",
		"OPERATOR_NETWORK_ENABLED", "OPERATOR_PEERING_ENABLED",
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

	// The parts without the whole is a real configuration — every tenant
	// adopted by hand — and saying so explicitly is how it stays meant.
	partsOnly := backendEnv(t, true,
		map[string]string{"OPERATOR_TENANT_ENABLED": "false"})
	if partsOnly["OPERATOR_TENANT_ENABLED"] != "false" {
		t.Errorf("an explicit false was overruled: %q",
			partsOnly["OPERATOR_TENANT_ENABLED"])
	}
	if partsOnly["OPERATOR_TENANT_ADDONS_ENABLED"] != "true" {
		t.Errorf("saying no to one tenant flag turned the others off: %v", partsOnly)
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
			"--set", "operator.config.ingressDomain=d",
			// Required by the network domain, which is on by default. The chart
			// refuses without it, which is the point of the guard beside this
			// one — so a test that renders a valid install has to supply it.
			"--set", "operator.config.tenantSupernet=10.200.0.0/14")
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

// TestTheChartCanConfigureEverythingTheOperatorNeeds.
//
// A VPC created through the operator came up attached, routed and healthy —
// and open to every other tenant, while the wizard's review step said
// "Isolated: Yes". Its own condition said why: `NoSupernet`. The operator takes
// the tenant supernet as a command-line flag, the developer's kustomize overlay
// passed it, and the chart did not — so the chart could install a network
// controller that cannot do the one thing the interface promises.
//
// One flag was noticed. Three were missing. This is the guard for the class: a
// site fact the operator takes must be reachable from the chart, and a new one
// added to main.go fails here until it is either wired or declared as something
// the chart deliberately does not set.
func TestTheChartCanConfigureEverythingTheOperatorNeeds(t *testing.T) {
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatalf("locating the repository: %v", err)
	}

	// Flags that are about the process rather than the site. They are set by the
	// chart's own template and are nobody's configuration.
	plumbing := map[string]bool{
		"leader-elect": true, "health-probe-bind-address": true,
		"metrics-bind-address": true, "metrics-secure": true, "domains": true,
		"metrics-cert-path": true, "metrics-cert-name": true, "metrics-cert-key": true,
		"webhook-cert-path": true, "webhook-cert-name": true, "webhook-cert-key": true,
		"enable-http2": true,
	}

	main, err := os.ReadFile(filepath.Join(root, "operator/cmd/main.go"))
	if err != nil {
		t.Fatalf("reading main.go: %v", err)
	}
	declared := regexp.MustCompile(`flag\.\w+Var\(&\w+, "([a-z0-9-]+)"`).
		FindAllStringSubmatch(string(main), -1)

	chart, err := os.ReadFile(filepath.Join(root,
		"helm/kubevirt-ui/templates/operator.yaml"))
	if err != nil {
		t.Fatalf("reading the operator template: %v", err)
	}

	for _, match := range declared {
		name := match[1]
		if plumbing[name] {
			continue
		}
		if !strings.Contains(string(chart), "--"+name+"=") {
			t.Errorf("the operator takes --%s and the chart cannot set it: an "+
				"install would run with it empty, and whatever that means "+
				"happens silently", name)
		}
	}
}

// TestAFlagWithNoControllerBehindItIsRefused.
//
// A handover flag names a controller that has to be running. Turning one on
// while its domain is off — or while the operator is not installed at all —
// hands the work to nobody: the product stops writing, nothing takes over, and
// the create reports success and produces nothing. It is the same shape as the
// supernet: a flag on without the thing it needs, failing quietly at the far
// end. Refused at render, where it is one line.
func TestAFlagWithNoControllerBehindItIsRefused(t *testing.T) {
	for _, c := range []struct {
		name, wants string
		args        []string
	}{{
		name:  "the domain that would do the work is off",
		wants: "operator.domains.network.enabled is false",
		args: []string{"--set", "operator.enabled=true",
			"--set", "operator.config.kubeOvnNamespace=k",
			"--set", "operator.config.metallbNamespace=m",
			"--set", "operator.config.metallbPool=p",
			"--set", "operator.config.cpTransitSubnet=c",
			"--set", "operator.config.ingressDomain=d",
			"--set", "operator.config.tenantSupernet=10.200.0.0/14",
			"--set", "operator.domains.network.enabled=false",
			"--set", "backend.env.OPERATOR_NETWORK_ENABLED=true"},
	}, {
		name:  "there is no operator at all",
		wants: "the operator is not installed",
		args:  []string{"--set", "backend.env.OPERATOR_VM_ENABLED=true"},
	}, {
		name:  "the network domain without a supernet",
		wants: "tenantSupernet is required",
		args: []string{"--set", "operator.enabled=true",
			"--set", "operator.config.kubeOvnNamespace=k",
			"--set", "operator.config.metallbNamespace=m",
			"--set", "operator.config.metallbPool=p",
			"--set", "operator.config.cpTransitSubnet=c",
			"--set", "operator.config.ingressDomain=d"},
	}, {
		name:  "one fact, two readers, disagreeing",
		wants: "they must agree",
		args: []string{"--set", "operator.enabled=true",
			"--set", "operator.config.kubeOvnNamespace=k",
			"--set", "operator.config.metallbNamespace=m",
			"--set", "operator.config.metallbPool=p",
			"--set", "operator.config.cpTransitSubnet=c",
			"--set", "operator.config.ingressDomain=d",
			"--set", "operator.config.tenantSupernet=10.200.0.0/14",
			"--set", "backend.env.TENANT_SUPERNET=10.99.0.0/14"},
	}} {
		t.Run(c.name, func(t *testing.T) {
			out, err := renderChart(t, c.args...)
			if err == nil {
				t.Fatalf("it rendered, and it should have refused:\n%s",
					firstLines(out, 5))
			}
			if !strings.Contains(out, c.wants) {
				t.Errorf("refused for the wrong reason — wanted %q in:\n%s",
					c.wants, firstLines(out, 5))
			}
		})
	}
}

func renderChart(t *testing.T, args ...string) (string, error) {
	t.Helper()
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatalf("locating the repository: %v", err)
	}
	full := append([]string{"template", "kubevirt-ui",
		filepath.Join(root, "helm", "kubevirt-ui")}, args...)
	out, err := exec.Command("helm", full...).CombinedOutput()
	return string(out), err
}

func firstLines(text string, n int) string {
	lines := strings.Split(text, "\n")
	if len(lines) > n {
		lines = lines[:n]
	}
	return strings.Join(lines, "\n")
}

// TestAdmissionIsWiredEndToEnd.
//
// Three things have to agree or the guard is worse than absent: the
// configuration must name a service the chart creates, that service must select
// the pods that actually serve 9443, and those pods must carry the certificate
// the CA injection refers to. Two of the three webhooks are
// `failurePolicy: Fail`, so a mismatch does not degrade validation — it rejects
// every ManagedTenant and ManagedVM write in the cluster.
//
// Asserted by reading the objects, not by looking for strings in the render.
// The first version of this test did the latter and two mutants walked through
// it: pointing the volume at the wrong secret, and selecting the wrong domain.
// Both left the same words on the page in some other object.
func TestAdmissionIsWiredEndToEnd(t *testing.T) {
	docs := renderObjects(t,
		"--set", "operator.enabled=true",
		"--set", "operator.webhooks.enabled=true",
		"--set", "operator.config.kubeOvnNamespace=k",
		"--set", "operator.config.metallbNamespace=m",
		"--set", "operator.config.metallbPool=p",
		"--set", "operator.config.cpTransitSubnet=c",
		"--set", "operator.config.ingressDomain=d",
		"--set", "operator.config.tenantSupernet=10.200.0.0/14")

	config := one(t, docs, "ValidatingWebhookConfiguration", "")
	service := one(t, docs, "Service", "kubevirt-ui-operator-webhook")
	cert := one(t, docs, "Certificate", "")
	vm := one(t, docs, "Deployment", "kubevirt-ui-operator-vm")

	// The configuration names the service the chart creates.
	for _, hook := range dig(config, "webhooks").([]any) {
		named := dig(hook, "clientConfig", "service", "name")
		if named != dig(service, "metadata", "name") {
			t.Errorf("a webhook points at %v, and the chart creates %v",
				named, dig(service, "metadata", "name"))
		}
	}

	// The service selects the pods of the domain that serves admission, and
	// they are the pods that open the port.
	if got := dig(service, "spec", "selector", "platform.kubevirt-ui.io/domain"); got != "vm" {
		t.Errorf("the webhook service selects domain %v, and only vm serves it", got)
	}
	container := dig(vm, "spec", "template", "spec", "containers").([]any)[0]
	if !hasPort(container, 9443) {
		t.Error("the vm deployment does not open 9443")
	}

	// The pods carry the certificate the injection refers to, and it is the one
	// this certificate issues.
	secret := dig(cert, "spec", "secretName")
	mounted := volumeSecret(t, vm, "/tmp/k8s-webhook-server/serving-certs")
	if mounted != secret {
		t.Errorf("the pods mount %q and the certificate writes %q", mounted, secret)
	}
	injected := dig(config, "metadata", "annotations", "cert-manager.io/inject-ca-from")
	want := fmt.Sprintf("%v/%v", dig(cert, "metadata", "namespace"),
		dig(cert, "metadata", "name"))
	if injected != want {
		t.Errorf("the CA is injected from %v, and the certificate is %v", injected, want)
	}
}

// TestAdmissionCannotBeAskedForWithoutTheDeploymentThatServesIt.
func TestAdmissionCannotBeAskedForWithoutTheDeploymentThatServesIt(t *testing.T) {
	out, err := renderChart(t,
		"--set", "operator.enabled=true",
		"--set", "operator.webhooks.enabled=true",
		"--set", "operator.domains.vm.enabled=false",
		"--set", "operator.config.kubeOvnNamespace=k",
		"--set", "operator.config.metallbNamespace=m",
		"--set", "operator.config.metallbPool=p",
		"--set", "operator.config.cpTransitSubnet=c",
		"--set", "operator.config.ingressDomain=d",
		"--set", "operator.config.tenantSupernet=10.200.0.0/14")
	if err == nil {
		t.Fatal("it rendered a webhook configuration with nothing behind it")
	}
	if !strings.Contains(out, "needs operator.domains.vm.enabled") {
		t.Errorf("refused for the wrong reason:\n%s", firstLines(out, 5))
	}
}

// TestWithAdmissionOffNothingIsLeftBehind.
//
// Half of it would be worse than none: a configuration with no server, or a
// server with no configuration and a certificate nobody uses.
func TestWithAdmissionOffNothingIsLeftBehind(t *testing.T) {
	out, err := renderChart(t,
		"--set", "operator.enabled=true",
		"--set", "operator.config.kubeOvnNamespace=k",
		"--set", "operator.config.metallbNamespace=m",
		"--set", "operator.config.metallbPool=p",
		"--set", "operator.config.cpTransitSubnet=c",
		"--set", "operator.config.ingressDomain=d",
		"--set", "operator.config.tenantSupernet=10.200.0.0/14")
	if err != nil {
		t.Fatalf("rendering: %v", err)
	}
	for _, absent := range []string{
		"ValidatingWebhookConfiguration", "kind: Certificate",
		"containerPort: 9443", "serving-certs",
	} {
		if strings.Contains(out, absent) {
			t.Errorf("%q is rendered with admission off", absent)
		}
	}
	if !strings.Contains(out, "ENABLE_WEBHOOKS") {
		t.Error("the binary is not told to keep its webhook server down")
	}
}

// renderObjects renders the chart and parses what comes out.
func renderObjects(t *testing.T, args ...string) []map[string]any {
	t.Helper()
	out, err := renderChart(t, args...)
	if err != nil {
		t.Fatalf("rendering: %v\n%s", err, firstLines(out, 5))
	}
	var docs []map[string]any
	for _, part := range strings.Split(out, "\n---") {
		var doc map[string]any
		if err := yaml.Unmarshal([]byte(part), &doc); err != nil || doc == nil {
			continue
		}
		docs = append(docs, doc)
	}
	if len(docs) < 5 {
		t.Fatalf("only %d objects parsed — the split is wrong", len(docs))
	}
	return docs
}

// one is the single object of a kind, optionally by name.
func one(t *testing.T, docs []map[string]any, kind, name string) map[string]any {
	t.Helper()
	var found []map[string]any
	for _, doc := range docs {
		if doc["kind"] != kind {
			continue
		}
		if name != "" && dig(doc, "metadata", "name") != name {
			continue
		}
		found = append(found, doc)
	}
	if len(found) != 1 {
		t.Fatalf("wanted one %s %q, found %d", kind, name, len(found))
	}
	return found[0]
}

func dig(doc any, path ...string) any {
	for _, key := range path {
		asMap, ok := doc.(map[string]any)
		if !ok {
			return nil
		}
		doc = asMap[key]
	}
	return doc
}

func hasPort(container any, port int) bool {
	ports, _ := dig(container, "ports").([]any)
	for _, entry := range ports {
		if value, ok := dig(entry, "containerPort").(float64); ok && int(value) == port {
			return true
		}
	}
	return false
}

// volumeSecret is the secret behind whatever is mounted at a path.
func volumeSecret(t *testing.T, deployment map[string]any, path string) string {
	t.Helper()
	pod := dig(deployment, "spec", "template", "spec")
	container := dig(pod, "containers").([]any)[0]
	name := ""
	mounts, _ := dig(container, "volumeMounts").([]any)
	for _, mount := range mounts {
		if dig(mount, "mountPath") == path {
			name, _ = dig(mount, "name").(string)
		}
	}
	if name == "" {
		t.Fatalf("nothing is mounted at %s", path)
	}
	volumes, _ := dig(pod, "volumes").([]any)
	for _, volume := range volumes {
		if dig(volume, "name") == name {
			secret, _ := dig(volume, "secret", "secretName").(string)
			return secret
		}
	}
	t.Fatalf("no volume called %q", name)
	return ""
}
