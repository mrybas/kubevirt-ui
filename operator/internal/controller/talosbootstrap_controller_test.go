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

package controller

import (
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// withCA and withoutCA are the two shapes of a rendered Talos worker config.
//
// `machine.ca` is present in both: it is the *Talos* CA and always there. The
// `cluster` branch is what the kubelet's certificate flow reads, and its
// absence is the whole defect — a worker that boots, runs a kubelet, and never
// joins, because nothing files its CSR.
const withoutCA = `machine:
  ca:
    crt: dGFsb3MtY2E=
  type: worker
cluster:
  controlPlane:
    endpoint: https://10.199.0.100:6443
`

const withCA = `machine:
  ca:
    crt: dGFsb3MtY2E=
  type: worker
cluster:
  ca:
    crt: azhzLWNh
  controlPlane:
    endpoint: https://10.199.0.100:6443
`

func mustTenantNamespace(t *testing.T, name, tenant string) {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name:   name,
		Labels: map[string]string{tenantLabel: tenant},
	}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating %s: %v", name, err)
	}
}

func mustWorkerTemplate(t *testing.T, namespace, name, data string) {
	t.Helper()
	tpl := &unstructured.Unstructured{}
	tpl.SetGroupVersionKind(talosConfigTemplateGVK)
	tpl.SetName(name)
	tpl.SetNamespace(namespace)
	// `generateType: none` the way the product writes it: every value is
	// already known, so there is nothing for the provider to generate.
	if err := unstructured.SetNestedMap(tpl.Object, map[string]any{
		"template": map[string]any{
			"spec": map[string]any{"generateType": "none", "data": data},
		},
	}, "spec"); err != nil {
		t.Fatalf("building the template: %v", err)
	}
	if err := k8sClient.Create(testCtx, tpl); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the template: %v", err)
	}
}

func mustMachineDeployment(t *testing.T, namespace, name, configRef string) {
	t.Helper()
	md := &unstructured.Unstructured{}
	md.SetGroupVersionKind(machineDeploymentGVK)
	md.SetName(name)
	md.SetNamespace(namespace)
	_ = unstructured.SetNestedMap(md.Object, map[string]any{
		"clusterName": strings.TrimSuffix(name, "-workers"),
		"selector":    map[string]any{},
		"template": map[string]any{
			"spec": map[string]any{
				"clusterName": strings.TrimSuffix(name, "-workers"),
				"bootstrap": map[string]any{
					"configRef": map[string]any{
						"apiVersion": talosConfigTemplateGVK.GroupVersion().String(),
						"kind":       "TalosConfigTemplate",
						"name":       configRef,
					},
				},
				"infrastructureRef": map[string]any{
					"apiVersion": "infrastructure.cluster.x-k8s.io/v1alpha1",
					"kind":       "KubevirtMachineTemplate",
					"name":       name,
				},
			},
		},
	}, "spec")
	if err := k8sClient.Create(testCtx, md); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the MachineDeployment: %v", err)
	}
}

func mustCASecret(t *testing.T, namespace, tenant string) {
	t.Helper()
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: tenant + "-ca", Namespace: namespace},
		Data:       map[string][]byte{"ca.crt": []byte("azhzLWNh")},
	}
	if err := k8sClient.Create(testCtx, secret); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the CA secret: %v", err)
	}
}

func configRefOf(t *testing.T, namespace, name string) string {
	t.Helper()
	md := &unstructured.Unstructured{}
	md.SetGroupVersionKind(machineDeploymentGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: namespace, Name: name,
	}, md); err != nil {
		t.Fatalf("reading the MachineDeployment: %v", err)
	}
	ref, _, _ := unstructured.NestedString(md.Object,
		"spec", "template", "spec", "bootstrap", "configRef", "name")
	return ref
}

// TestABrokenWorkerTemplateIsRepaired is the safeguard, and the only way to
// know a safeguard works is to break the thing it guards.
//
// A TalosConfigTemplate is immutable, so the repair is a new object and a
// MachineDeployment repointed at it — which is also why the defect was
// permanent once it happened.
func TestABrokenWorkerTemplateIsRepaired(t *testing.T) {
	mustTenantNamespace(t, "tenant-brk", "brk")
	mustCASecret(t, "tenant-brk", "brk")
	mustWorkerTemplate(t, "tenant-brk", "brk-workers", withoutCA)
	mustMachineDeployment(t, "tenant-brk", "brk-workers", "brk-workers")

	eventually(t, "the replacement template", func() error {
		tpl := &unstructured.Unstructured{}
		tpl.SetGroupVersionKind(talosConfigTemplateGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-brk", Name: "brk-workers-ca",
		}, tpl); err != nil {
			return err
		}
		data, _, _ := unstructured.NestedString(tpl.Object,
			"spec", "template", "spec", "data")
		if !strings.Contains(data, "azhzLWNh") {
			return fmt.Errorf("the replacement has no cluster CA:\n%s", data)
		}
		// Every field of the original survives — this is a repair, not a
		// regeneration, and `generateType` is required by the CRD.
		if generate, _, _ := unstructured.NestedString(tpl.Object,
			"spec", "template", "spec", "generateType"); generate != "none" {
			return fmt.Errorf("generateType = %q", generate)
		}
		for _, keep := range []string{"dGFsb3MtY2E=", "10.199.0.100"} {
			if !strings.Contains(data, keep) {
				return fmt.Errorf("the repair lost %q:\n%s", keep, data)
			}
		}
		return nil
	})

	eventually(t, "the workers to be repointed", func() error {
		if got := configRefOf(t, "tenant-brk", "brk-workers"); got != "brk-workers-ca" {
			return fmt.Errorf("configRef = %q", got)
		}
		return nil
	})

	// The original is left alone. It is immutable, and rewriting history is not
	// the repair.
	original := &unstructured.Unstructured{}
	original.SetGroupVersionKind(talosConfigTemplateGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-brk", Name: "brk-workers",
	}, original); err != nil {
		t.Fatalf("the original was removed: %v", err)
	}
}

// TestAHealthyTemplateIsNotTouched.
func TestAHealthyTemplateIsNotTouched(t *testing.T) {
	mustTenantNamespace(t, "tenant-ok", "ok")
	mustCASecret(t, "tenant-ok", "ok")
	mustWorkerTemplate(t, "tenant-ok", "ok-workers", withCA)
	mustMachineDeployment(t, "tenant-ok", "ok-workers", "ok-workers")

	consistently(t, "no replacement and no repointing", 4*time.Second, func() error {
		tpl := &unstructured.Unstructured{}
		tpl.SetGroupVersionKind(talosConfigTemplateGVK)
		err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-ok", Name: "ok-workers-ca",
		}, tpl)
		if err == nil {
			return fmt.Errorf("a replacement was written for a healthy template")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		if got := configRefOf(t, "tenant-ok", "ok-workers"); got != "ok-workers" {
			return fmt.Errorf("configRef moved to %q", got)
		}
		return nil
	})
}

// TestNothingIsWrittenWhileTheCAIsAbsent.
//
// The wait at create time makes this unlikely; when it happens, writing a
// template without a CA would bake the defect in permanently — and the
// replacement is immutable too.
func TestNothingIsWrittenWhileTheCAIsAbsent(t *testing.T) {
	mustTenantNamespace(t, "tenant-noca", "noca")
	mustWorkerTemplate(t, "tenant-noca", "noca-workers", withoutCA)
	mustMachineDeployment(t, "tenant-noca", "noca-workers", "noca-workers")

	consistently(t, "no replacement without a CA to put in it", 4*time.Second, func() error {
		tpl := &unstructured.Unstructured{}
		tpl.SetGroupVersionKind(talosConfigTemplateGVK)
		err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-noca", Name: "noca-workers-ca",
		}, tpl)
		if err == nil {
			return fmt.Errorf("a CA-less replacement was written")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	// And when the CA arrives, the repair follows without anything else
	// happening — that is the reason the secret is watched.
	mustCASecret(t, "tenant-noca", "noca")

	eventually(t, "the repair to follow the CA", func() error {
		if got := configRefOf(t, "tenant-noca", "noca-workers"); got != "noca-workers-ca" {
			return fmt.Errorf("configRef = %q", got)
		}
		return nil
	})
}

// TestANamespaceThatIsNotATenantIsIgnored. The label is the fact; the
// `tenant-` prefix is a convention, and this controller writes machine
// configuration.
func TestANamespaceThatIsNotATenantIsIgnored(t *testing.T) {
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "tenant-lookalike"}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the namespace: %v", err)
	}
	// Everything a repair needs, including the CA — so that the only thing
	// stopping it is the missing label. Without the secret this test passed for
	// the wrong reason: nothing was written because there was nothing to write
	// with, and removing the label check changed nothing.
	mustCASecret(t, "tenant-lookalike", "lookalike")
	mustWorkerTemplate(t, "tenant-lookalike", "lookalike-workers", withoutCA)
	mustMachineDeployment(t, "tenant-lookalike", "lookalike-workers", "lookalike-workers")

	consistently(t, "nothing written in a namespace with no tenant label", 4*time.Second,
		func() error {
			tpl := &unstructured.Unstructured{}
			tpl.SetGroupVersionKind(talosConfigTemplateGVK)
			err := k8sClient.Get(testCtx, types.NamespacedName{
				Namespace: "tenant-lookalike", Name: "lookalike-workers-ca",
			}, tpl)
			if err == nil {
				return fmt.Errorf("a replacement was written")
			}
			if !apierrors.IsNotFound(err) {
				return err
			}
			return nil
		})
}

// TestReconcileItselfRefusesAnUnlabelledNamespace calls the reconciler
// directly, with no predicate in the way.
//
// The watch filter and the check inside Reconcile are two different things, and
// only one of them is a safety property. A predicate is wiring: it can be
// widened by adding a Watch somewhere else — which is exactly what happens
// here, since the template, deployment and secret watches all map by namespace
// and carry no label filter of their own. So the refusal has to hold when the
// function is called on its own.
func TestReconcileItselfRefusesAnUnlabelledNamespace(t *testing.T) {
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "tenant-direct"}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the namespace: %v", err)
	}
	// Everything a repair needs, so nothing else can be the reason it does not
	// happen.
	mustCASecret(t, "tenant-direct", "direct")
	mustWorkerTemplate(t, "tenant-direct", "direct-workers", withoutCA)
	mustMachineDeployment(t, "tenant-direct", "direct-workers", "direct-workers")

	reconciler := &TalosBootstrapReconciler{
		Client: k8sClient,
		Scheme: k8sClient.Scheme(),
	}
	result, err := reconciler.Reconcile(testCtx, ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "tenant-direct"},
	})
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if result.Requeue || result.RequeueAfter != 0 {
		t.Errorf("it asked to come back: %+v", result)
	}

	replacement := &unstructured.Unstructured{}
	replacement.SetGroupVersionKind(talosConfigTemplateGVK)
	err = k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-direct", Name: "direct-workers-ca",
	}, replacement)
	if err == nil {
		t.Fatal("a replacement was written for a namespace carrying no tenant label")
	}
	if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the replacement: %v", err)
	}
	if got := configRefOf(t, "tenant-direct", "direct-workers"); got != "direct-workers" {
		t.Errorf("the MachineDeployment was repointed to %q", got)
	}

	// And the same call on a labelled namespace does repair, so the test is not
	// passing because the direct invocation does nothing at all.
	mustTenantNamespace(t, "tenant-direct-ok", "direct-ok")
	mustCASecret(t, "tenant-direct-ok", "direct-ok")
	mustWorkerTemplate(t, "tenant-direct-ok", "direct-ok-workers", withoutCA)
	mustMachineDeployment(t, "tenant-direct-ok", "direct-ok-workers", "direct-ok-workers")

	if _, err := reconciler.Reconcile(testCtx, ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "tenant-direct-ok"},
	}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	repaired := &unstructured.Unstructured{}
	repaired.SetGroupVersionKind(talosConfigTemplateGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-direct-ok", Name: "direct-ok-workers-ca",
	}, repaired); err != nil {
		t.Fatalf("the labelled namespace was not repaired either: %v", err)
	}
}

// repairAnnouncements is how many times a repair was announced for one
// namespace, counting the aggregation Kubernetes does for repeats.
//
// Events for a cluster-scoped object are filed in `default`, since there is no
// namespace of their own to put them in.
func repairAnnouncements(t *testing.T, namespace string) int32 {
	t.Helper()
	events := &corev1.EventList{}
	if err := k8sClient.List(testCtx, events, client.InNamespace("default")); err != nil {
		t.Fatalf("listing events: %v", err)
	}
	var total int32
	for _, event := range events.Items {
		if event.Reason != "WorkerBootstrapRepaired" ||
			event.InvolvedObject.Name != namespace {
			continue
		}
		if event.Count == 0 {
			total++
			continue
		}
		total += event.Count
	}
	return total
}

// TestARepairIsAnnouncedOnceAndNotOnEveryPass.
//
// The original template is immutable, so it stays broken for the tenant's whole
// life and every wake finds it broken again. The first version said "wrote a
// replacement and repointed the MachineDeployment" each of those times — on the
// live stand two identical lines a second apart, one of which wrote nothing —
// which turns a real repair into a warning nobody can pick out from the noise.
//
// Counted through the manager's own recorder rather than one built here: the
// running manager reacts to every poke this test makes, so a private reconciler
// would be racing it for who does the work, and losing that race looks exactly
// like silence.
func TestARepairIsAnnouncedOnceAndNotOnEveryPass(t *testing.T) {
	mustTenantNamespace(t, "tenant-once", "once")
	mustCASecret(t, "tenant-once", "once")
	mustWorkerTemplate(t, "tenant-once", "once-workers", withoutCA)
	mustMachineDeployment(t, "tenant-once", "once-workers", "once-workers")

	eventually(t, "the repair to be announced", func() error {
		if got := configRefOf(t, "tenant-once", "once-workers"); got != "once-workers-ca" {
			return fmt.Errorf("configRef = %q", got)
		}
		// Non-vacuity, and the same read the assertion below uses: if the
		// announcement never arrived, a count that stays put proves nothing.
		if got := repairAnnouncements(t, "tenant-once"); got != 1 {
			return fmt.Errorf("announcements = %d, want the one repair", got)
		}
		return nil
	})

	// Wake it repeatedly. Each pass finds the original still broken — it always
	// will — and has nothing left to do about it.
	for poke := 1; poke <= 5; poke++ {
		// Patched rather than read-then-updated: this client reads from a
		// cache, and five updates in a row race their own watch.
		secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
			Namespace: "tenant-once", Name: "once-ca",
		}}
		patch := []byte(fmt.Sprintf(
			`{"metadata":{"annotations":{"poke":%q}}}`, fmt.Sprint(poke)))
		if err := k8sClient.Patch(testCtx, secret,
			client.RawPatch(types.MergePatchType, patch)); err != nil {
			t.Fatalf("poke %d: %v", poke, err)
		}
	}

	consistently(t, "the repair announced once and only once", 5*time.Second, func() error {
		if got := repairAnnouncements(t, "tenant-once"); got != 1 {
			return fmt.Errorf("announcements = %d after five wakes", got)
		}
		return nil
	})
}

// TestATerminatingNamespaceIsLeftAlone.
//
// A namespace being torn down refuses new content, so the repair can only fail
// — and it failed loudly, once per backoff, for as long as termination took.
// Measured on the stand: eleven `Reconciler error` lines in six seconds.
func TestATerminatingNamespaceIsLeftAlone(t *testing.T) {
	// Unlabelled at first, so nothing repairs it before it is dying: the label
	// is what makes it this controller's business.
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "tenant-dying"}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the namespace: %v", err)
	}
	mustCASecret(t, "tenant-dying", "dying")
	mustWorkerTemplate(t, "tenant-dying", "dying-workers", withoutCA)
	mustMachineDeployment(t, "tenant-dying", "dying-workers", "dying-workers")

	if err := k8sClient.Delete(testCtx, ns); err != nil {
		t.Fatalf("deleting the namespace: %v", err)
	}
	// envtest runs no namespace controller, so it stays Terminating — which is
	// the state under test.
	eventually(t, "the namespace to be terminating", func() error {
		live := &corev1.Namespace{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "tenant-dying"}, live); err != nil {
			return err
		}
		if live.DeletionTimestamp == nil {
			return fmt.Errorf("it is not terminating yet")
		}
		if live.Labels[tenantLabel] == "" {
			live.Labels = map[string]string{tenantLabel: "dying"}
			return k8sClient.Update(testCtx, live)
		}
		return nil
	})

	recorder := record.NewFakeRecorder(16)
	reconciler := &TalosBootstrapReconciler{
		Client:   k8sClient,
		Scheme:   k8sClient.Scheme(),
		Recorder: recorder,
	}
	result, err := reconciler.Reconcile(testCtx, ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "tenant-dying"},
	})
	if err != nil {
		t.Fatalf("a dying namespace produced an error to retry: %v", err)
	}
	if result.Requeue || result.RequeueAfter != 0 {
		t.Errorf("it asked to come back to a dying namespace: %+v", result)
	}
	select {
	case event := <-recorder.Events:
		t.Errorf("it announced something about a dying namespace: %s", event)
	default:
	}

	// And nothing was written into it. The error above is the loud symptom;
	// this is the property — whether the API server refuses the write or
	// happens to allow it, the controller does not attempt it.
	replacement := &unstructured.Unstructured{}
	replacement.SetGroupVersionKind(talosConfigTemplateGVK)
	err = k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-dying", Name: "dying-workers-ca",
	}, replacement)
	if err == nil {
		t.Error("a replacement was written into a namespace being torn down")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the replacement: %v", err)
	}
	if got := configRefOf(t, "tenant-dying", "dying-workers"); got != "dying-workers" {
		t.Errorf("the workers of a dying tenant were repointed to %q", got)
	}
}
