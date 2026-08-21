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

// Package addons renders the Flux releases a tenant's cluster is built from.
//
// One renderer, deliberately. There were two: the create path and the
// reconcile loop's repair, and they did not agree — the repair omitted
// `install.disableWait`, whose absence is documented one file over as the thing
// that wedged a lab tenant's CNI in `uninstalling` for ever. It fires only when
// a release is missing, which is exactly the state a fresh tenant is in, which
// is exactly when omitting it wedges.
package addons

import (
	"fmt"
	"sort"
	"strings"

	"sigs.k8s.io/yaml"
)

// Parameter is one knob a component exposes.
type Parameter struct {
	ID      string `json:"id"`
	Default string `json:"default,omitempty"`
}

// Component is one entry in the catalogue.
type Component struct {
	ID            string         `json:"id"`
	Name          string         `json:"name,omitempty"`
	Category      string         `json:"category,omitempty"`
	Required      bool           `json:"required,omitempty"`
	ChartPath     string         `json:"chartPath,omitempty"`
	Namespace     string         `json:"namespace,omitempty"`
	DefaultValues map[string]any `json:"defaultValues,omitempty"`
	Parameters    []Parameter    `json:"parameters,omitempty"`
}

// Catalog is what the deployment offers, read from the ConfigMap that already
// states it — the same one the product reads, because two copies of a
// catalogue is two answers to "which chart".
type Catalog struct {
	GitRepositoryRef map[string]string `json:"gitRepositoryRef,omitempty"`
	BasePath         string            `json:"basePath,omitempty"`
	Components       []Component       `json:"components,omitempty"`
}

// ParseCatalog reads the `catalog.yaml` entry of the catalogue ConfigMap.
func ParseCatalog(raw string) (Catalog, error) {
	catalog := Catalog{BasePath: "base"}
	if strings.TrimSpace(raw) == "" {
		return catalog, nil
	}
	if err := yaml.Unmarshal([]byte(raw), &catalog); err != nil {
		return Catalog{}, fmt.Errorf("the addon catalogue is not readable: %w", err)
	}
	if catalog.BasePath == "" {
		catalog.BasePath = "base"
	}
	return catalog, nil
}

// Find is the component with this id.
func (c Catalog) Find(id string) (Component, bool) {
	for _, component := range c.Components {
		if component.ID == id {
			return component, true
		}
	}
	return Component{}, false
}

// Request is one addon a tenant asked for.
type Request struct {
	ID         string
	Parameters map[string]string
}

// Release is a rendered Flux HelmRelease.
type Release struct {
	Name      string
	Namespace string
	Labels    map[string]string
	Spec      map[string]any
}

// Render turns the tenant's chosen addons into releases, in dependency order.
//
// The chain is namespaces → CNI → everything else, and it is expressed as
// Flux's own `dependsOn` rather than by ordering the writes: a tenant's CNI
// cannot install before the namespaces its chart targets exist, and nothing
// else can install before there is a network.
func Render(tenant, namespace string, catalog Catalog, requested []Request) []Release {
	var namespacesID, cniID string
	for _, component := range catalog.Components {
		if component.ID == "namespaces" {
			namespacesID = component.ID
		}
		if component.Required && component.Category == "networking" {
			cniID = component.ID
		}
	}

	// The namespaces chart is handed every namespace the other addons target,
	// so it can create them before anything needs them.
	var targets []any
	for _, request := range requested {
		if component, found := catalog.Find(request.ID); found && component.Namespace != "" {
			targets = append(targets, map[string]any{"name": component.Namespace})
		}
	}

	var out []Release
	for _, request := range requested {
		component, found := catalog.Find(request.ID)
		if !found {
			// A tenant asking for something this deployment does not offer is
			// the catalogue's answer, not a reason to fail the rest.
			continue
		}

		values := helmValues(tenant, namespace, component, request.Parameters)
		if component.ID == "namespaces" {
			values = map[string]any{"namespaces": targets}
		}

		var dependsOn []any
		switch {
		case component.ID == "namespaces":
			dependsOn = nil
		case component.Required && component.Category == "networking":
			if namespacesID != "" {
				dependsOn = []any{map[string]any{
					"name": tenant + "-" + namespacesID, "namespace": namespace,
				}}
			}
		default:
			if cniID != "" {
				dependsOn = []any{map[string]any{
					"name": tenant + "-" + cniID, "namespace": namespace,
				}}
			}
		}

		out = append(out, Release{
			Name:      tenant + "-" + component.ID,
			Namespace: namespace,
			Labels: map[string]string{
				"kubevirt-ui.io/tenant": tenant,
				"kubevirt-ui.io/addon":  component.ID,
			},
			Spec: releaseSpec(tenant, namespace, catalog, component, values, dependsOn),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// releaseSpec is the HelmRelease body, and every field in it is load-bearing
// somewhere.
func releaseSpec(
	tenant, namespace string, catalog Catalog, component Component,
	values map[string]any, dependsOn []any,
) map[string]any {
	source := map[string]any{"kind": "GitRepository"}
	for key, value := range catalog.GitRepositoryRef {
		source[key] = value
	}

	spec := map[string]any{
		"interval":         "30m",
		"timeout":          "15m",
		"releaseName":      component.ID,
		"storageNamespace": "kube-system",
		// The tenant's own admin credential, by the key that reaches its API
		// from inside this cluster.
		"kubeConfig": map[string]any{"secretRef": map[string]any{
			"name": tenant + "-admin-kubeconfig", "key": "super-admin.svc",
		}},
		"chart": map[string]any{"spec": map[string]any{
			"chart":     "./" + catalog.BasePath + "/" + component.ChartPath,
			"sourceRef": source,
			"interval":  "12h",
		}},
		"install": map[string]any{
			"crds":            "CreateReplace",
			"createNamespace": true,
			"remediation":     map[string]any{"retries": int64(5)},
			// A fresh tenant has no Ready node until its CNI is up, and its CNI
			// is one of these releases — so waiting for workloads is waiting for
			// something this install is supposed to cause. With the wait on, the
			// install times out, Flux remediates by uninstalling, and the
			// uninstall's hook pod has nowhere to run either: the release then
			// sits in `uninstalling` for ever and never recovers, even once
			// nodes appear. Measured on the lab with zero nodes registered.
			"disableWait": true,
		},
		"upgrade": map[string]any{
			"crds":        "CreateReplace",
			"remediation": map[string]any{"retries": int64(5)},
		},
	}
	if component.Namespace != "" {
		spec["targetNamespace"] = component.Namespace
	}
	if len(values) > 0 {
		spec["values"] = values
	}
	if len(dependsOn) > 0 {
		spec["dependsOn"] = dependsOn
	}
	return spec
}

// helmValues merges the component's defaults with what the tenant asked for.
//
// Two components need more than a merge, and both reasons were measured rather
// than designed.
func helmValues(
	tenant, namespace string, component Component, asked map[string]string,
) map[string]any {
	params := map[string]string{}
	for _, parameter := range component.Parameters {
		params[parameter.ID] = parameter.Default
	}
	for key, value := range asked {
		if value != "" {
			params[key] = value
		}
	}

	values := deepCopy(component.DefaultValues)

	switch component.ID {
	case "alloy":
		// The collector's configuration is a template string, not a tree, so
		// the three values it needs are substituted into it.
		config, _ := nested(values, "alloy", "alloy", "configMap")
		if config == nil {
			return values
		}
		content, _ := config["content"].(string)
		if content == "" {
			return values
		}
		if url := params["VM_REMOTE_WRITE_URL"]; url != "" {
			content = strings.ReplaceAll(content, `url = ""`, `url = "`+url+`"`)
			content = strings.ReplaceAll(content, `cluster = ""`, `cluster = "`+tenant+`"`)
			interval := params["SCRAPE_INTERVAL"]
			if interval == "" {
				interval = "30s"
			}
			content = strings.ReplaceAll(content,
				`scrape_interval = "30s"`, `scrape_interval = "`+interval+`"`)
			config["content"] = content
		}
		return values

	case "kubevirt-csi-driver":
		// An empty namespace is not a default, it is a broken driver: every
		// CreateVolume then reaches the host API with a resource name and no
		// namespace and comes back saying so. The tenant's own namespace is the
		// only correct value, and it is derivable, so it is never left blank.
		infraNS := params["INFRA_CLUSTER_NAMESPACE"]
		if infraNS == "" {
			infraNS = namespace
		}
		values["infraClusterNamespace"] = infraNS
		if class := params["INFRA_STORAGE_CLASS_NAME"]; class != "" {
			classes, _ := values["storageClasses"].([]any)
			if len(classes) == 0 {
				classes = []any{map[string]any{}}
			}
			if first, ok := classes[0].(map[string]any); ok {
				first["infraStorageClassName"] = class
			}
			values["storageClasses"] = classes
		}
		secret, _ := values["infraClusterKubeconfigSecret"].(map[string]any)
		if secret == nil {
			secret = map[string]any{}
		}
		if _, set := secret["name"]; !set {
			secret["name"] = "infra-cluster-credentials"
		}
		if _, set := secret["key"]; !set {
			secret["key"] = "kubeconfig"
		}
		values["infraClusterKubeconfigSecret"] = secret
		return values
	}
	return values
}

func nested(values map[string]any, path ...string) (map[string]any, bool) {
	current := values
	for _, key := range path {
		next, ok := current[key].(map[string]any)
		if !ok {
			return nil, false
		}
		current = next
	}
	return current, true
}

func deepCopy(in map[string]any) map[string]any {
	out := map[string]any{}
	for key, value := range in {
		switch typed := value.(type) {
		case map[string]any:
			out[key] = deepCopy(typed)
		case []any:
			list := make([]any, len(typed))
			for i, item := range typed {
				if nested, ok := item.(map[string]any); ok {
					list[i] = deepCopy(nested)
					continue
				}
				list[i] = item
			}
			out[key] = list
		default:
			out[key] = value
		}
	}
	return out
}
