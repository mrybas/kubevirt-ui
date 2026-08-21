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
	"os"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/addons"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

var helmReleaseGVK = schema.GroupVersionKind{
	Group: "helm.toolkit.fluxcd.io", Version: "v2", Kind: "HelmRelease",
}

const (
	catalogNamespaceEnv = "TENANTS_ADDON_CATALOG_NAMESPACE"
	catalogNameEnv      = "TENANTS_ADDON_CATALOG_CONFIGMAP"
	defaultCatalogNS    = "flux-system"
	defaultCatalogName  = "tenant-addon-catalog"
)

// reconcileAddons writes what the tenant's cluster is built from.
//
// The catalogue is read from the ConfigMap that already states it — the same
// one the product reads — because two copies of a catalogue are two answers to
// "which chart, at which version".
func (r *ManagedTenantReconciler) reconcileAddons(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) (ready bool, reason, message string, err error) {
	catalog, found, err := r.addonCatalog(ctx)
	if err != nil {
		return false, "", "", err
	}
	if !found {
		return false, "NoCatalogue", fmt.Sprintf(
			"no addon catalogue at %s/%s, so there is nothing to install from",
			r.catalogNamespace(), r.catalogName()), nil
	}

	requested := wantedAddons(obj, catalog)
	if len(requested) == 0 {
		return true, "", "", nil
	}

	rendered := addons.Render(obj.Name, namespace, catalog, requested)
	if err := r.retireAddons(ctx, obj, namespace, rendered); err != nil {
		return false, "", "", err
	}

	for _, release := range rendered {
		live := &unstructured.Unstructured{}
		live.SetGroupVersionKind(helmReleaseGVK)
		live.SetName(release.Name)
		live.SetNamespace(release.Namespace)
		spec := release.Spec
		labels := release.Labels
		grows := release.CreatesNamespaces
		if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
			mergeLabels(live, labels)
			// Laid over what is there rather than replacing it: Flux writes its
			// own defaults into this object, and stripping them every pass
			// while it writes them back is a loop with nothing changing.
			existing, _, _ := unstructured.NestedMap(live.Object, "spec")
			merged := kube.MergeSpec(existing, spec)
			if grows {
				merged = keepNamespaces(existing, merged)
			}
			return unstructured.SetNestedMap(live.Object, merged, "spec")
		}); err != nil {
			return false, "", "", fmt.Errorf("writing %s: %w", release.Name, err)
		}
	}

	return r.addonState(ctx, obj, namespace)
}

// keepNamespaces makes the namespace list grow and never shrink.
//
// Everywhere else here, rendering the whole thing from what is wanted is the
// discipline: it is how the sediment of an addon nobody enabled any more gets
// swept up. This one release is the exception, and the exception was measured.
// The list is a Helm manifest, Helm prunes what a new revision stops
// rendering, and these resources are Namespaces **in the tenant's own
// cluster** — so shrinking the list does not tidy a record, it deletes a
// namespace and everything the tenant put in it.
//
// Two ways that showed up on the stand, from one earlier pass of this code:
// one tenant's `alloy` namespace was removed and quietly deleted, and another's
// `kube-system` was removed, refused by the API server, and left the release
// wedged with every addon that depends on it stuck behind a failed upgrade.
//
// So the cost is accepted in the other direction: an entry nobody needs stays
// in the list, and an unwanted namespace goes away when somebody deletes it on
// purpose, not as a side effect of describing something else.
func keepNamespaces(existing, merged map[string]any) map[string]any {
	before, found, err := unstructured.NestedSlice(existing, "values", "namespaces")
	if !found || err != nil {
		return merged
	}
	after, _, err := unstructured.NestedSlice(merged, "values", "namespaces")
	if err != nil {
		return merged
	}

	named := func(entry any) (string, bool) {
		fields, ok := entry.(map[string]any)
		if !ok {
			return "", false
		}
		name, ok := fields["name"].(string)
		return name, ok
	}

	held := map[string]bool{}
	for _, entry := range after {
		if name, ok := named(entry); ok {
			held[name] = true
		}
	}
	for _, entry := range before {
		name, ok := named(entry)
		// An entry nobody can read a name out of is kept as it stands: it is
		// still a resource in somebody's cluster, and dropping what this code
		// does not understand is how the deletion happened in the first place.
		if !ok || !held[name] {
			after = append(after, entry)
			if ok {
				held[name] = true
			}
		}
	}

	out := merged
	if err := unstructured.SetNestedSlice(out, after, "values", "namespaces"); err != nil {
		return merged
	}
	return out
}

// retireAddons removes the releases of addons the tenant no longer wants.
//
// The other half of writing them, and it is the half that goes missing: on the
// stand a tenant still lists `uat-t1-alloy` among the namespaces its cluster
// should have, months after the addon was disabled and its release deleted.
// Nothing removed the entry, because the thing that added it only ever added.
//
// The namespace list is a different matter: it is rendered from what is wanted
// but never shrunk when it is written, because removing an entry deletes a
// namespace in the tenant's cluster — see keepNamespaces. What needs saying
// explicitly here is the release itself. Only
// ours — by the label this operator puts on them — because a HelmRelease in
// this namespace that nobody here wrote belongs to somebody else.
func (r *ManagedTenantReconciler) retireAddons(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace string, rendered []addons.Release,
) error {
	wanted := map[string]bool{}
	for _, release := range rendered {
		wanted[release.Name] = true
	}

	releases := &unstructured.UnstructuredList{}
	releases.SetGroupVersionKind(helmReleaseGVK.GroupVersion().WithKind("HelmReleaseList"))
	if err := r.List(ctx, releases, client.InNamespace(namespace),
		client.MatchingLabels{"kubevirt-ui.io/tenant": obj.Name}); err != nil {
		return fmt.Errorf("reading the tenant's releases: %w", err)
	}
	for i := range releases.Items {
		item := &releases.Items[i]
		if wanted[item.GetName()] || item.GetLabels()["kubevirt-ui.io/addon"] == "" {
			continue
		}
		if err := r.Delete(ctx, item); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("retiring %s: %w", item.GetName(), err)
		}
		kube.CountWrite(r.Scheme, item, tenantControllerName, "deleted")
	}
	return nil
}

// wantedAddons is what the tenant asked for, plus what the catalogue says a
// tenant cannot do without.
//
// Required components are not a default the caller may drop: a tenant without
// its CNI is not a smaller tenant, it is one whose nodes will never be Ready,
// and the page would say nothing about why.
func wantedAddons(
	obj *platformv1alpha1.ManagedTenant, catalog addons.Catalog,
) []addons.Request {
	asked := map[string]map[string]string{}
	var order []string
	for _, addon := range obj.Spec.Addons {
		if _, seen := asked[addon.ID]; !seen {
			order = append(order, addon.ID)
		}
		asked[addon.ID] = addon.Parameters
	}
	for _, component := range catalog.Components {
		if !component.Required {
			continue
		}
		if _, seen := asked[component.ID]; !seen {
			asked[component.ID] = nil
			order = append(order, component.ID)
		}
	}

	out := make([]addons.Request, 0, len(order))
	for _, id := range order {
		out = append(out, addons.Request{ID: id, Parameters: asked[id]})
	}
	return out
}

// addonState reads what Flux made of them.
//
// One condition for the set with the stuck releases named, because a tenant is
// not half-built and the useful question is which release is stuck.
func (r *ManagedTenantReconciler) addonState(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) (ready bool, reason, message string, err error) {
	releases := &unstructured.UnstructuredList{}
	releases.SetGroupVersionKind(helmReleaseGVK.GroupVersion().WithKind("HelmReleaseList"))
	if err := r.List(ctx, releases, client.InNamespace(namespace),
		client.MatchingLabels{"kubevirt-ui.io/tenant": obj.Name}); err != nil {
		return false, "", "", fmt.Errorf("reading the tenant's releases: %w", err)
	}

	var installing, failed []string
	for i := range releases.Items {
		item := &releases.Items[i]
		state, note := releaseState(item)
		switch state {
		case "ready":
		case "failed":
			failed = append(failed, item.GetName()+" ("+note+")")
		default:
			installing = append(installing, item.GetName())
		}
	}
	sort.Strings(failed)
	sort.Strings(installing)

	switch {
	case len(failed) > 0:
		// Named rather than counted, and it does not stop the pass: the tenant
		// beside this one has nothing to do with a chart that will not install.
		return false, "AddonFailed", "will not install: " + strings.Join(failed, ", "), nil
	case len(installing) > 0:
		return false, "Installing", "installing: " + strings.Join(installing, ", "), nil
	default:
		return true, "Installed", fmt.Sprintf("%d release(s)", len(releases.Items)), nil
	}
}

// releaseState reads Flux's own answer rather than guessing from the spec.
func releaseState(release *unstructured.Unstructured) (state, note string) {
	conditions, _, _ := unstructured.NestedSlice(release.Object, "status", "conditions")
	for _, raw := range conditions {
		condition, _ := raw.(map[string]any)
		if condition == nil || condition["type"] != "Ready" {
			continue
		}
		switch condition["status"] {
		case "True":
			return "ready", ""
		case "False":
			reason, _ := condition["reason"].(string)
			if reason == "" {
				reason = "not ready"
			}
			return "failed", reason
		}
	}
	return "installing", ""
}

func (r *ManagedTenantReconciler) addonCatalog(
	ctx context.Context,
) (addons.Catalog, bool, error) {
	config := &corev1.ConfigMap{}
	err := r.reader().Get(ctx, types.NamespacedName{
		Namespace: r.catalogNamespace(), Name: r.catalogName(),
	}, config)
	if err != nil {
		if unreadable(err) {
			return addons.Catalog{}, false, nil
		}
		return addons.Catalog{}, false, fmt.Errorf("reading the addon catalogue: %w", err)
	}
	catalog, err := addons.ParseCatalog(config.Data["catalog.yaml"])
	if err != nil {
		return addons.Catalog{}, false, err
	}
	return catalog, true, nil
}

func (r *ManagedTenantReconciler) catalogNamespace() string {
	if r.CatalogNamespace != "" {
		return r.CatalogNamespace
	}
	if name := os.Getenv(catalogNamespaceEnv); name != "" {
		return name
	}
	return defaultCatalogNS
}

func (r *ManagedTenantReconciler) catalogName() string {
	if name := os.Getenv(catalogNameEnv); name != "" {
		return name
	}
	return defaultCatalogName
}

func addonsCondition(ready bool, reason, message string) metav1.Condition {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	if reason == "" {
		reason = "Installing"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionAddonsReady,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}
