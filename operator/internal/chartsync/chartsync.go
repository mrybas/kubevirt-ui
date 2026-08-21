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

// Package chartsync renders the operator's own generated manifests into the
// product's Helm chart.
//
// The CRDs and the manager ClusterRole come from kubebuilder markers in this
// module's Go code. Copying them into the chart by hand makes two records of
// one fact, and it is the copy that goes stale — quietly, because a chart that
// installs last year's schema still installs, and the tenant whose new field
// the API server prunes is the one who finds out.
//
// One implementation, used by both the command that writes the files and the
// test that refuses a diff. It was briefly a Python script, which failed for a
// duller reason worth remembering: the container the Go suite runs in has no
// yaml module, so the guard could not run where the guard lives.
package chartsync

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"sigs.k8s.io/yaml"
)

const banner = `# Generated from %s by "go run ./cmd/chartsync".
# Do not edit: the markers in the operator's Go code are the source, and a
# hand-edit here is a second record of one fact that nothing compares.
`

// Files are the chart templates this package owns, keyed by their path relative
// to the repository root.
func Files(root string) (map[string]string, error) {
	crds, err := renderCRDs(filepath.Join(root, "operator/config/crd/bases"))
	if err != nil {
		return nil, err
	}
	role, err := renderRole(filepath.Join(root, "operator/config/rbac/role.yaml"))
	if err != nil {
		return nil, err
	}
	return map[string]string{
		"helm/kubevirt-ui/templates/operator-crds.yaml":         crds,
		"helm/kubevirt-ui/templates/operator-manager-role.yaml": role,
	}, nil
}

func renderCRDs(dir string) (string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return "", fmt.Errorf("reading the CRD directory: %w", err)
	}
	var names []string
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".yaml") {
			names = append(names, entry.Name())
		}
	}
	sort.Strings(names)

	var out strings.Builder
	fmt.Fprintf(&out, banner, "operator/config/crd/bases")
	out.WriteString("{{- if and .Values.operator.enabled .Values.operator.crds.install }}\n")
	for _, name := range names {
		raw, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return "", err
		}
		var doc map[string]any
		if err := yaml.Unmarshal(raw, &doc); err != nil {
			return "", fmt.Errorf("reading %s: %w", name, err)
		}
		// Kept on uninstall. These definitions hold the tenants, the networks
		// and the VMs; removing them would cascade-delete every object
		// described by them, which is not what uninstalling a UI means.
		metadata, _ := doc["metadata"].(map[string]any)
		if metadata == nil {
			metadata = map[string]any{}
			doc["metadata"] = metadata
		}
		annotations, _ := metadata["annotations"].(map[string]any)
		if annotations == nil {
			annotations = map[string]any{}
			metadata["annotations"] = annotations
		}
		annotations["helm.sh/resource-policy"] = "keep"

		body, err := yaml.Marshal(doc)
		if err != nil {
			return "", err
		}
		out.WriteString("---\n")
		out.Write(body)
	}
	out.WriteString("{{- end }}\n")
	return out.String(), nil
}

func renderRole(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("reading the manager role: %w", err)
	}
	var role map[string]any
	if err := yaml.Unmarshal(raw, &role); err != nil {
		return "", fmt.Errorf("reading the manager role: %w", err)
	}
	body, err := yaml.Marshal(map[string]any{
		"apiVersion": "rbac.authorization.k8s.io/v1",
		"kind":       "ClusterRole",
		"metadata":   map[string]any{"name": "{{ .Release.Name }}-operator-manager"},
		"rules":      role["rules"],
	})
	if err != nil {
		return "", err
	}
	var out strings.Builder
	fmt.Fprintf(&out, banner, "operator/config/rbac/role.yaml")
	out.WriteString("{{- if .Values.operator.enabled }}\n---\n")
	out.Write(body)
	out.WriteString("{{- end }}\n")
	return out.String(), nil
}

// Drifted lists the files whose contents no longer match what would be
// generated, writing them out unless check is set.
func Drifted(root string, check bool) ([]string, error) {
	files, err := Files(root)
	if err != nil {
		return nil, err
	}
	var drifted []string
	for name, want := range files {
		path := filepath.Join(root, name)
		got, err := os.ReadFile(path)
		if err == nil && string(got) == want {
			continue
		}
		drifted = append(drifted, name)
		if !check {
			if err := os.WriteFile(path, []byte(want), 0o644); err != nil {
				return nil, err
			}
		}
	}
	sort.Strings(drifted)
	return drifted, nil
}
