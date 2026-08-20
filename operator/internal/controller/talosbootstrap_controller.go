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
	"context"
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/yaml"

	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

const (
	bootstrapControllerName = "talosbootstrap"

	// repairedSuffix names the replacement template.
	//
	// A TalosConfigTemplate is immutable, so repair means a *new* object and a
	// MachineDeployment repointed at it — which is also why the defect was
	// permanent once it happened: nothing could patch the original.
	repairedSuffix = "-workers-ca"

	// tenantLabel marks a namespace as a tenant's.
	tenantLabel = "kubevirt-ui.io/tenant"
)

var (
	talosConfigTemplateGVK = schema.GroupVersionKind{
		Group: "bootstrap.cluster.x-k8s.io", Version: "v1alpha3", Kind: "TalosConfigTemplate",
	}
	machineDeploymentGVK = schema.GroupVersionKind{
		Group: "cluster.x-k8s.io", Version: "v1beta1", Kind: "MachineDeployment",
	}
)

// TalosBootstrapReconciler repairs worker templates that carry no Kubernetes CA.
//
// Without `cluster.ca` in its machine config a Talos worker boots, runs a
// kubelet, and never joins: nothing files its CSR, so the node does not exist
// as far as the cluster is concerned while the VM looks perfectly healthy.
//
// The repair itself is a port. What changes is where it runs: it was a call in
// a loop inside the request-serving backend, on a timer, with no leader
// election and no watch — so two replicas ran it twice, one replica ran it
// never after a restart, and a template broken a second after the pass was
// broken for thirty seconds at best. Here a write to either object wakes it.
type TalosBootstrapReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
}

// +kubebuilder:rbac:groups=bootstrap.cluster.x-k8s.io,resources=talosconfigtemplates,verbs=get;list;watch;create
// +kubebuilder:rbac:groups=cluster.x-k8s.io,resources=machinedeployments,verbs=get;list;watch;patch;update

// Reconcile repairs one tenant's worker bootstrap, if it needs it.
//
// The request names the namespace; the tenant is the label on it, because the
// namespace name is a convention and the label is the fact.
func (r *TalosBootstrapReconciler) Reconcile(
	ctx context.Context, req ctrl.Request,
) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	namespace := &corev1.Namespace{}
	if err := r.Get(ctx, types.NamespacedName{Name: req.Name}, namespace); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	tenant := namespace.Labels[tenantLabel]
	if tenant == "" {
		return ctrl.Result{}, nil
	}

	template := &unstructured.Unstructured{}
	template.SetGroupVersionKind(talosConfigTemplateGVK)
	err := r.Get(ctx, types.NamespacedName{
		Namespace: req.Name, Name: tenant + "-workers",
	}, template)
	if apierrors.IsNotFound(err) {
		// A cloud-init tenant, or nothing built yet. Neither is this
		// controller's business.
		return ctrl.Result{}, nil
	}
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("reading the worker template: %w", err)
	}

	config, lacks, err := templateLacksClusterCA(template)
	if err != nil {
		// Unparseable is not the same as missing. Rewriting a config this
		// controller cannot read would be worse than leaving it.
		logger.Info("the worker template is not readable; leaving it alone",
			"tenant", tenant, "reason", err.Error())
		return ctrl.Result{}, nil
	}
	if !lacks {
		return ctrl.Result{}, nil
	}

	ca, err := r.tenantCA(ctx, tenant, req.Name)
	if err != nil {
		return ctrl.Result{}, err
	}
	if ca == "" {
		// The wait at create time makes this unlikely; when it happens, the CA
		// simply is not there yet and writing a template without one would
		// bake the defect in permanently.
		logger.Info("the worker template has no Kubernetes CA and the CA secret "+
			"is still absent; leaving both alone", "tenant", tenant)
		return ctrl.Result{}, nil
	}

	repaired := tenant + repairedSuffix
	if err := r.writeRepairedTemplate(ctx, template, config, repaired, ca); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.repointDeployment(ctx, req.Name, tenant, repaired); err != nil {
		return ctrl.Result{}, err
	}

	logger.Info("the worker bootstrap template had no Kubernetes CA; wrote a "+
		"replacement and repointed the MachineDeployment",
		"tenant", tenant, "template", repaired)
	r.event(namespace, "Warning", "WorkerBootstrapRepaired",
		fmt.Sprintf("%s-workers carried no cluster CA, so its workers would boot "+
			"and never join; %s was written and the MachineDeployment repointed",
			tenant, repaired))
	return ctrl.Result{}, nil
}

// templateLacksClusterCA reads the rendered config and says whether the
// Kubernetes CA is missing.
//
// `machine.ca` is the *Talos* CA and is always there; it is the `cluster`
// branch that the kubelet's certificate flow reads, and its absence is the
// whole defect. Checking the wrong one of the two would report every template
// as healthy.
func templateLacksClusterCA(template *unstructured.Unstructured) (map[string]any, bool, error) {
	data, found, err := unstructured.NestedString(
		template.Object, "spec", "template", "spec", "data")
	if err != nil || !found || data == "" {
		return nil, false, fmt.Errorf("no rendered config in the template")
	}
	config := map[string]any{}
	if err := yaml.Unmarshal([]byte(data), &config); err != nil {
		return nil, false, fmt.Errorf("the config is not YAML: %w", err)
	}
	cluster, _ := config["cluster"].(map[string]any)
	if cluster == nil {
		return config, true, nil
	}
	if cluster["ca"] != nil || cluster["acceptedCAs"] != nil {
		return config, false, nil
	}
	return config, true, nil
}

// tenantCA is the tenant cluster's Kubernetes CA, base64 as Talos wants it.
// Kamaji keeps it encoded already, so the value passes through untouched.
func (r *TalosBootstrapReconciler) tenantCA(
	ctx context.Context, tenant, namespace string,
) (string, error) {
	secret := &corev1.Secret{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: tenant + "-ca",
	}, secret)
	if apierrors.IsNotFound(err) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("reading %s/%s-ca: %w", namespace, tenant, err)
	}
	return string(secret.Data["ca.crt"]), nil
}

// writeRepairedTemplate creates the replacement, leaving the original alone.
func (r *TalosBootstrapReconciler) writeRepairedTemplate(
	ctx context.Context, original *unstructured.Unstructured,
	config map[string]any, name, ca string,
) error {
	cluster, _ := config["cluster"].(map[string]any)
	if cluster == nil {
		cluster = map[string]any{}
	}
	cluster["ca"] = map[string]any{"crt": ca}
	cluster["acceptedCAs"] = []any{map[string]any{"crt": ca}}
	config["cluster"] = cluster

	rendered, err := yaml.Marshal(config)
	if err != nil {
		return fmt.Errorf("rendering the repaired config: %w", err)
	}

	repaired := &unstructured.Unstructured{Object: map[string]any{}}
	repaired.SetGroupVersionKind(talosConfigTemplateGVK)
	repaired.SetName(name)
	repaired.SetNamespace(original.GetNamespace())
	labels := map[string]string{}
	for key, value := range original.GetLabels() {
		labels[key] = value
	}
	labels["kubevirt-ui.io/managed"] = "true"
	repaired.SetLabels(labels)

	spec, _, _ := unstructured.NestedMap(original.Object, "spec")
	if spec == nil {
		spec = map[string]any{}
	}
	if err := unstructured.SetNestedField(
		spec, string(rendered), "template", "spec", "data"); err != nil {
		return err
	}
	if err := unstructured.SetNestedMap(repaired.Object, spec, "spec"); err != nil {
		return err
	}

	if err := r.Create(ctx, repaired); err != nil {
		if apierrors.IsAlreadyExists(err) {
			// Written by an earlier pass, or by the backend loop while both are
			// running. Either way the MachineDeployment still has to point at
			// it, so this is not a reason to stop.
			return nil
		}
		return fmt.Errorf("creating %s: %w", name, err)
	}
	kube.CountWrite(r.Scheme, repaired, bootstrapControllerName, "created")
	return nil
}

// repointDeployment sends the workers at the repaired template. CAPI rolls them
// itself from there.
func (r *TalosBootstrapReconciler) repointDeployment(
	ctx context.Context, namespace, tenant, template string,
) error {
	deployment := &unstructured.Unstructured{}
	deployment.SetGroupVersionKind(machineDeploymentGVK)
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: tenant + "-workers",
	}, deployment); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("reading the MachineDeployment: %w", err)
	}

	current, _, _ := unstructured.NestedString(deployment.Object,
		"spec", "template", "spec", "bootstrap", "configRef", "name")
	if current == template {
		return nil
	}

	patched := deployment.DeepCopy()
	if err := unstructured.SetNestedField(patched.Object, template,
		"spec", "template", "spec", "bootstrap", "configRef", "name"); err != nil {
		return err
	}
	if err := r.Patch(ctx, patched, client.MergeFrom(deployment)); err != nil {
		return fmt.Errorf("repointing the MachineDeployment: %w", err)
	}
	kube.CountWrite(r.Scheme, patched, bootstrapControllerName, "updated")
	return nil
}

func (r *TalosBootstrapReconciler) event(
	obj client.Object, kind, reason, message string,
) {
	if r.Recorder != nil {
		r.Recorder.Event(obj, kind, reason, message)
	}
}

// SetupWithManager wakes the controller on either object it looks at.
//
// Keyed by namespace, because that is what identifies a tenant here and both
// objects live in one.
func (r *TalosBootstrapReconciler) SetupWithManager(mgr ctrl.Manager) error {
	toNamespace := handler.EnqueueRequestsFromMapFunc(
		func(ctx context.Context, obj client.Object) []reconcile.Request {
			if obj.GetNamespace() == "" {
				return nil
			}
			return []reconcile.Request{{
				NamespacedName: types.NamespacedName{Name: obj.GetNamespace()},
			}}
		})

	templates := &unstructured.Unstructured{}
	templates.SetGroupVersionKind(talosConfigTemplateGVK)
	deployments := &unstructured.Unstructured{}
	deployments.SetGroupVersionKind(machineDeploymentGVK)
	secrets := &corev1.Secret{}

	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Namespace{}, builder.WithPredicates(
			predicate.NewPredicateFuncs(func(o client.Object) bool {
				return o.GetLabels()[tenantLabel] != ""
			}))).
		Watches(templates, toNamespace).
		Watches(deployments, toNamespace).
		// The CA arriving is the event that makes a repair possible, and
		// waiting for the next resync instead would leave workers not joining
		// for no reason.
		Watches(secrets, toNamespace, builder.WithPredicates(
			predicate.NewPredicateFuncs(func(o client.Object) bool {
				return strings.HasSuffix(o.GetName(), "-ca")
			}))).
		Named(bootstrapControllerName).
		Complete(r)
}
