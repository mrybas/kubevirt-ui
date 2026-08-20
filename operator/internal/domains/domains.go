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

// Package domains splits the operator into independently deployable halves.
//
// One binary, several Deployments: the tenant controllers must not be able to
// stop VM reconciliation by crashing, and the VM service account must not carry
// the rights the tenant controllers need. Each Deployment runs the same image
// with a different --domains value, its own ServiceAccount, and — critically —
// its own leader-election lease, so two profiles never fight over one lock.
package domains

import (
	"fmt"
	"sort"
	"strings"
)

const (
	// VM covers images, templates, virtual machines and their operations.
	VM = "vm"
	// Network covers VPCs, subnets, peerings, announcements, egress, underlay.
	Network = "network"
	// Tenant covers the CAPI/Kamaji/Talos tenant clusters.
	Tenant = "tenant"
	// Remediation covers node remediation (restart, not replace).
	Remediation = "remediation"
)

// All lists every known domain, in the order they are introduced by the plan.
var All = []string{VM, Network, Tenant, Remediation}

// Set is the collection of domains a single process was asked to run.
type Set map[string]struct{}

// Parse turns a comma-separated flag value into a Set.
//
// An unknown domain is a hard error rather than a warning: a typo in a
// Deployment argument would otherwise start a manager that silently runs
// nothing, and "no controller is watching" looks exactly like "everything is
// already reconciled".
func Parse(value string) (Set, error) {
	known := make(map[string]struct{}, len(All))
	for _, d := range All {
		known[d] = struct{}{}
	}

	out := Set{}
	for _, raw := range strings.Split(value, ",") {
		name := strings.TrimSpace(raw)
		if name == "" {
			continue
		}
		if _, ok := known[name]; !ok {
			return nil, fmt.Errorf("unknown domain %q (known: %s)", name, strings.Join(All, ", "))
		}
		out[name] = struct{}{}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("no domains selected (known: %s)", strings.Join(All, ", "))
	}
	return out, nil
}

// Has reports whether the domain is enabled in this process.
func (s Set) Has(domain string) bool {
	_, ok := s[domain]
	return ok
}

// Sorted returns the enabled domains in a stable order.
func (s Set) Sorted() []string {
	out := make([]string, 0, len(s))
	for d := range s {
		out = append(out, d)
	}
	sort.Strings(out)
	return out
}

// String renders the enabled set for logs.
func (s Set) String() string {
	return strings.Join(s.Sorted(), ",")
}

// LeaderElectionID derives a lease name from the enabled set, so that a
// vm-profile Deployment and a network-profile Deployment do not contend for the
// same lock and accidentally shut each other down.
func (s Set) LeaderElectionID() string {
	return strings.Join(s.Sorted(), "-") + ".operator.kubevirt-ui.io"
}
