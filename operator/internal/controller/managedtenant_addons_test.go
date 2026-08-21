package controller

import (
	"fmt"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

const testCatalog = `
gitRepositoryRef:
  name: flux-system
  namespace: flux-system
basePath: tenant-charts
components:
  - id: namespaces
    name: Namespaces
    category: core
    required: true
    chartPath: core/namespaces
    namespace: default
  - id: calico
    name: Calico
    category: networking
    required: true
    chartPath: networking/calico
    namespace: tigera-operator
  - id: alloy
    name: Alloy
    category: observability
    chartPath: observability/alloy
    namespace: alloy
`

func mustCatalog(t *testing.T, namespace string) {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating %s: %v", namespace, err)
	}
	config := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: "tenant-addon-catalog",
	}}
	config.Data = map[string]string{"catalog.yaml": testCatalog}
	if err := k8sClient.Create(testCtx, config); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting the catalogue: %v", err)
	}
}

func addonReconciler(catalogNS string) *ManagedTenantReconciler {
	return &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
		CatalogNamespace: catalogNS,
	}
}

func releasesOf(t *testing.T, namespace, tenant string) map[string]*unstructured.Unstructured {
	t.Helper()
	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(helmReleaseGVK.GroupVersion().WithKind("HelmReleaseList"))
	if err := k8sReader.List(testCtx, list); err != nil {
		t.Fatalf("listing releases: %v", err)
	}
	out := map[string]*unstructured.Unstructured{}
	for i := range list.Items {
		item := &list.Items[i]
		if item.GetNamespace() == namespace &&
			item.GetLabels()["kubevirt-ui.io/tenant"] == tenant {
			out[item.GetName()] = item
		}
	}
	return out
}

// TestWhatTheClusterCannotDoWithoutIsInstalledUnasked.
//
// A tenant without its CNI is not a smaller tenant: its nodes never go Ready
// and the page says nothing about why. The catalogue marks those components
// required, and required is not a default the caller may drop.
func TestWhatTheClusterCannotDoWithoutIsInstalledUnasked(t *testing.T) {
	mustCatalog(t, "cat-a")
	mustNamespace(t, "tenant-tad1", "")

	obj := talosTenant("tad1")
	obj.Spec.Addons = nil // asked for nothing at all

	ready, reason, message, err := addonReconciler("cat-a").reconcileAddons(
		testCtx, obj, "tenant-tad1")
	if err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}
	if ready {
		t.Errorf("nothing has installed yet, so this cannot be ready: %s %s", reason, message)
	}

	got := releasesOf(t, "tenant-tad1", "tad1")
	if len(got) != 2 {
		t.Fatalf("releases = %v, want the two required ones", keysOf(got))
	}
	for _, name := range []string{"tad1-namespaces", "tad1-calico"} {
		if _, found := got[name]; !found {
			t.Errorf("%s was not installed", name)
		}
	}
	// And the chain is expressed to Flux rather than by ordering the writes:
	// the CNI cannot install before the namespaces its chart targets exist.
	depends, _, _ := unstructured.NestedSlice(got["tad1-calico"].Object, "spec", "dependsOn")
	if len(depends) != 1 {
		t.Fatalf("dependsOn = %v", depends)
	}
	first, _ := depends[0].(map[string]any)
	if first["name"] != "tad1-namespaces" {
		t.Errorf("the CNI depends on %v", first)
	}
}

// TestAnAskedForAddonJoinsTheRequiredOnes.
func TestAnAskedForAddonJoinsTheRequiredOnes(t *testing.T) {
	mustCatalog(t, "cat-b")
	mustNamespace(t, "tenant-tad2", "")

	obj := talosTenant("tad2")
	obj.Spec.Addons = []platformv1alpha1.TenantAddon{{
		ID: "alloy", Parameters: map[string]string{"VM_REMOTE_WRITE_URL": "https://vm/write"},
	}}

	if _, _, _, err := addonReconciler("cat-b").reconcileAddons(
		testCtx, obj, "tenant-tad2"); err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}
	got := releasesOf(t, "tenant-tad2", "tad2")
	if len(got) != 3 {
		t.Fatalf("releases = %v, want the two required plus alloy", keysOf(got))
	}
	// Anything that is not the CNI waits for the CNI: there is no network
	// before it.
	depends, _, _ := unstructured.NestedSlice(got["tad2-alloy"].Object, "spec", "dependsOn")
	first, _ := depends[0].(map[string]any)
	if first["name"] != "tad2-calico" {
		t.Errorf("alloy depends on %v", first)
	}
}

// TestOneStuckReleaseIsNamedAndTheRestKeepGoing.
//
// The plan asks for it in those words: a tenant with an undeployable addon goes
// Degraded with a reason, and its neighbours keep reconciling. Counting the
// failures would answer the wrong question — which one is stuck is the only
// useful thing to say.
func TestOneStuckReleaseIsNamedAndTheRestKeepGoing(t *testing.T) {
	mustCatalog(t, "cat-c")
	mustNamespace(t, "tenant-tad3", "")
	obj := talosTenant("tad3")

	reconciler := addonReconciler("cat-c")
	if _, _, _, err := reconciler.reconcileAddons(testCtx, obj, "tenant-tad3"); err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}

	// Flux's part: one installed, one that will not.
	setReleaseReady(t, "tenant-tad3", "tad3-namespaces", "True", "InstallSucceeded")
	setReleaseReady(t, "tenant-tad3", "tad3-calico", "False", "InstallFailed")

	ready, reason, message, err := reconciler.reconcileAddons(testCtx, obj, "tenant-tad3")
	if err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}
	if ready || reason != "AddonFailed" {
		t.Fatalf("ready=%v reason=%q", ready, reason)
	}
	if !strings.Contains(message, "tad3-calico") || !strings.Contains(message, "InstallFailed") {
		t.Errorf("the message names neither the release nor why: %s", message)
	}
	if strings.Contains(message, "tad3-namespaces") {
		t.Errorf("it named a release that installed fine: %s", message)
	}

	// The neighbour is untouched by any of it.
	mustNamespace(t, "tenant-tad4", "")
	neighbour := talosTenant("tad4")
	if _, _, _, err := reconciler.reconcileAddons(testCtx, neighbour, "tenant-tad4"); err != nil {
		t.Fatalf("the neighbour stopped reconciling: %v", err)
	}
	if len(releasesOf(t, "tenant-tad4", "tad4")) != 2 {
		t.Error("the neighbour did not get its own releases")
	}
}

func setReleaseReady(t *testing.T, namespace, name, status, reason string) {
	t.Helper()
	release := &unstructured.Unstructured{}
	release.SetGroupVersionKind(helmReleaseGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: namespace, Name: name,
	}, release); err != nil {
		t.Fatalf("reading %s: %v", name, err)
	}
	_ = unstructured.SetNestedSlice(release.Object, []any{map[string]any{
		"type": "Ready", "status": status, "reason": reason,
		"lastTransitionTime": "2026-08-21T00:00:00Z", "message": reason,
	}}, "status", "conditions")
	if err := k8sClient.Status().Update(testCtx, release); err != nil {
		t.Fatalf("setting %s ready=%s: %v", name, status, err)
	}
}

func keysOf(in map[string]*unstructured.Unstructured) []string {
	var out []string
	for key := range in {
		out = append(out, key)
	}
	return out
}

// TestNoCatalogueIsSaidRatherThanGuessed.
func TestNoCatalogueIsSaidRatherThanGuessed(t *testing.T) {
	mustNamespace(t, "tenant-tad5", "")
	ready, reason, message, err := addonReconciler("cat-missing").reconcileAddons(
		testCtx, talosTenant("tad5"), "tenant-tad5")
	if err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}
	if ready || reason != "NoCatalogue" {
		t.Errorf("ready=%v reason=%q", ready, reason)
	}
	if !strings.Contains(message, "cat-missing") {
		t.Errorf("the message does not say where it looked: %s", message)
	}
	_ = fmt.Sprint()
}

// TestAnAddonNoLongerWantedIsRetiredAndLeavesNoSediment.
//
// Measured on the stand: a tenant still lists `uat-t1-alloy` among the
// namespaces its cluster should have, long after the addon was disabled and its
// release deleted. Nothing removed the entry, because the thing that added it
// only ever added — and the namespace it names is not even the one the addon
// installed into, so the tenant carries an empty namespace named after
// something it does not have.
//
// Rendering the whole set every pass makes the list follow by construction. The
// release has to be said out loud.
func TestAnAddonNoLongerWantedIsRetiredAndLeavesNoSediment(t *testing.T) {
	mustCatalog(t, "cat-d")
	mustNamespace(t, "tenant-tad6", "")

	obj := talosTenant("tad6")
	obj.Spec.Addons = []platformv1alpha1.TenantAddon{{ID: "alloy"}}
	reconciler := addonReconciler("cat-d")
	if _, _, _, err := reconciler.reconcileAddons(testCtx, obj, "tenant-tad6"); err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}
	if _, found := releasesOf(t, "tenant-tad6", "tad6")["tad6-alloy"]; !found {
		t.Fatal("alloy was not installed in the first place")
	}
	namespacesOf := func() string {
		list, _, _ := unstructured.NestedSlice(
			releasesOf(t, "tenant-tad6", "tad6")["tad6-namespaces"].Object,
			"spec", "values", "namespaces")
		return fmt.Sprint(list)
	}
	if !strings.Contains(namespacesOf(), "alloy") {
		t.Fatalf("the namespace list does not mention alloy: %s", namespacesOf())
	}

	// Disabled.
	obj.Spec.Addons = nil
	if _, _, _, err := reconciler.reconcileAddons(testCtx, obj, "tenant-tad6"); err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}

	eventually(t, "the release to be retired", func() error {
		if _, found := releasesOf(t, "tenant-tad6", "tad6")["tad6-alloy"]; found {
			return fmt.Errorf("it is still there")
		}
		return nil
	})
	// And the sediment with it: nothing asks the tenant's cluster for that
	// namespace any more.
	if strings.Contains(namespacesOf(), "alloy") {
		t.Errorf("the namespace list still asks for it: %s", namespacesOf())
	}
	// What is required stays, disabled or not.
	got := releasesOf(t, "tenant-tad6", "tad6")
	if len(got) != 2 {
		t.Errorf("releases = %v, want the two required ones", keysOf(got))
	}
}

// TestARelease NobodyHereWroteIsLeftAlone.
func TestAReleaseNobodyHereWroteIsLeftAlone(t *testing.T) {
	mustCatalog(t, "cat-e")
	mustNamespace(t, "tenant-tad7", "")

	foreign := &unstructured.Unstructured{}
	foreign.SetGroupVersionKind(helmReleaseGVK)
	foreign.SetName("tad7-somebody-elses")
	foreign.SetNamespace("tenant-tad7")
	foreign.SetLabels(map[string]string{"kubevirt-ui.io/tenant": "tad7"})
	_ = unstructured.SetNestedMap(foreign.Object, map[string]any{
		"interval": "30m",
		"chart": map[string]any{"spec": map[string]any{
			"chart": "./x", "sourceRef": map[string]any{
				"kind": "GitRepository", "name": "flux-system",
			},
		}},
	}, "spec")
	if err := k8sClient.Create(testCtx, foreign); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting a foreign release: %v", err)
	}

	if _, _, _, err := addonReconciler("cat-e").reconcileAddons(
		testCtx, talosTenant("tad7"), "tenant-tad7"); err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}
	if _, found := releasesOf(t, "tenant-tad7", "tad7")["tad7-somebody-elses"]; !found {
		t.Error("it retired a release it did not write")
	}
}

// TestARelease FluxHasDefaultedIsNotRewrittenForever.
//
// The live check that found this: a HelmRelease on the stand carries
// `chart.spec.reconcileStrategy` that nothing here renders. Replacing the spec
// strips it, Flux writes it back, and the two rewrite each other for ever with
// nothing changing — visible only as a resourceVersion that never settles.
func TestAReleaseFluxHasDefaultedIsNotRewrittenForever(t *testing.T) {
	mustCatalog(t, "cat-f")
	mustNamespace(t, "tenant-tad8", "")

	obj := talosTenant("tad8")
	reconciler := addonReconciler("cat-f")
	if _, _, _, err := reconciler.reconcileAddons(testCtx, obj, "tenant-tad8"); err != nil {
		t.Fatalf("reconcileAddons: %v", err)
	}

	// Flux's part.
	release := &unstructured.Unstructured{}
	release.SetGroupVersionKind(helmReleaseGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tad8", Name: "tad8-calico",
	}, release); err != nil {
		t.Fatalf("reading the release: %v", err)
	}
	_ = unstructured.SetNestedField(release.Object, "ChartVersion",
		"spec", "chart", "spec", "reconcileStrategy")
	if err := k8sClient.Update(testCtx, release); err != nil {
		t.Fatalf("defaulting it: %v", err)
	}
	settled := release.GetResourceVersion()

	for pass := 1; pass <= 3; pass++ {
		if _, _, _, err := reconciler.reconcileAddons(testCtx, obj, "tenant-tad8"); err != nil {
			t.Fatalf("pass %d: %v", pass, err)
		}
	}

	after := &unstructured.Unstructured{}
	after.SetGroupVersionKind(helmReleaseGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tad8", Name: "tad8-calico",
	}, after); err != nil {
		t.Fatalf("reading it back: %v", err)
	}
	strategy, _, _ := unstructured.NestedString(after.Object,
		"spec", "chart", "spec", "reconcileStrategy")
	if strategy != "ChartVersion" {
		t.Errorf("it stripped what Flux wrote back: %q", strategy)
	}
	if after.GetResourceVersion() != settled {
		t.Errorf("three passes moved resourceVersion %s -> %s, so it is "+
			"rewriting an object nothing asked it to change",
			settled, after.GetResourceVersion())
	}
}
