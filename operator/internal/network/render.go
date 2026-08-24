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

// Package network renders a tenant network's kube-ovn objects.
//
// Kept free of a client so the shapes can be compared against what the UI
// writes today without a cluster in the way — which is the whole acceptance
// test for this migration.
package network

import (
	"fmt"
	"net/netip"
	"strings"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

const (
	// ManagedLabel marks everything this operator builds.
	ManagedLabel = "kubevirt-ui.io/managed"
	// RoleLabel carries the declared role, never an inferred one.
	RoleLabel = "kubevirt-ui.io/role"
	// TenantLabel, FolderLabel and EnvironmentLabel are how the tenant wizard
	// finds the networks a given (folder, environment) pair may use.
	TenantLabel      = "kubevirt-ui.io/tenant"
	FolderLabel      = "kubevirt-ui.io/folder"
	EnvironmentLabel = "kubevirt-ui.io/environment"

	// IsolationOptOutAnnotation and its value are how a network records that
	// somebody chose not to isolate it.
	//
	// Written only for that answer. Its absence means no choice was recorded,
	// and the reconciler isolates — the old default ran the other way, and
	// silence read as consent to stay open.
	IsolationOptOutAnnotation = "kubevirt-ui.io/isolation"
	IsolationOptOutValue      = "disabled"
)

// DefaultSubnetName is the subnet created with the VPC.
//
// Derived, not allocated: two concurrent creates cannot disagree about a name
// neither of them chose.
func DefaultSubnetName(net *platformv1alpha1.ManagedNetwork) string {
	return net.Name + "-default"
}

// Gateway is the configured gateway, or the first host of the block.
func Gateway(net *platformv1alpha1.ManagedNetwork) (string, error) {
	if net.Spec.Gateway != "" {
		return net.Spec.Gateway, nil
	}
	prefix, err := netip.ParsePrefix(net.Spec.CIDR)
	if err != nil {
		return "", fmt.Errorf("spec.cidr %q is not a CIDR: %w", net.Spec.CIDR, err)
	}
	return prefix.Masked().Addr().Next().String(), nil
}

// IsIsolated defaults to true. A network whose isolation was never stated is
// isolated, because the alternative default is one nobody notices.
func IsIsolated(net *platformv1alpha1.ManagedNetwork) bool {
	return net.Spec.Isolated == nil || *net.Spec.Isolated
}

// Attachments are the external subnets this VPC gets a router port on.
func Attachments(net *platformv1alpha1.ManagedNetwork) []string {
	if net.Spec.ExternalPlane == nil {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, name := range net.Spec.ExternalPlane.Attachments {
		name = strings.TrimSpace(name)
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		out = append(out, name)
	}
	// The egress subnet is an attachment whether or not it was listed twice:
	// a default route into a subnet the VPC has no port on is a route to
	// nowhere, and the two halves are one decision.
	if egress := strings.TrimSpace(net.Spec.ExternalPlane.EgressSubnet); egress != "" && !seen[egress] {
		out = append(out, egress)
	}
	return out
}

// Labels are stamped on both the VPC and its default subnet.
func Labels(net *platformv1alpha1.ManagedNetwork) map[string]string {
	labels := map[string]string{ManagedLabel: "true"}
	for key, value := range map[string]string{
		RoleLabel:        net.Spec.Role,
		TenantLabel:      net.Spec.Tenant,
		FolderLabel:      net.Spec.Folder,
		EnvironmentLabel: net.Spec.Environment,
	} {
		if value != "" {
			labels[key] = value
		}
	}
	return labels
}

// Route is a static route reduced to the fields anything here decides.
//
// kube-ovn stores three more (`bfdId`, `ecmpMode`, `routeTable`) which it
// defaults to empty strings and which nothing in this product sets. Comparing
// whole route objects would therefore never match, and the render would be
// rewritten on every pass — the same invisible write loop the link-watcher
// DaemonSet had, in a different object.
type Route struct {
	CIDR      string
	NextHopIP string
	Policy    string
}

// DesiredRoutes are the routes this network declares, the default route
// included when an egress subnet was named.
func DesiredRoutes(net *platformv1alpha1.ManagedNetwork, nextHop string) []Route {
	out := make([]Route, 0, len(net.Spec.StaticRoutes)+1)
	hasDefault := false
	for _, route := range net.Spec.StaticRoutes {
		policy := route.Policy
		if policy == "" {
			policy = "policyDst"
		}
		if route.CIDR == "0.0.0.0/0" {
			hasDefault = true
		}
		out = append(out, Route{CIDR: route.CIDR, NextHopIP: route.NextHopIP, Policy: policy})
	}
	// The default route is what puts the network on the external plane, and the
	// announcement generator reads exactly this: a network is advertised when
	// its default route leads into the external subnet. Datapath and
	// announcement are the same fact, so they cannot drift apart.
	if nextHop != "" && !hasDefault {
		out = append(out, Route{CIDR: "0.0.0.0/0", NextHopIP: nextHop, Policy: "policyDst"})
	}
	return out
}

// MergeRoutes adds the desired routes to the live list without disturbing it.
//
// Additive on purpose, and this is a limitation rather than a preference: VPC
// peering writes into this same list, so a controller that replaced it would
// delete the other writer's routes on its first pass. A live route matching a
// desired one on the fields above is kept verbatim, defaults and all, so
// adopting a network the product already built is a no-op.
//
// The consequence, stated rather than hidden: removing a route from the CR does
// not remove it from the VPC. That closes when peering moves here too and the
// list has one writer.
func MergeRoutes(liveRoutes []any, desired []Route) ([]any, bool) {
	out := append([]any(nil), liveRoutes...)
	changed := false
	for _, want := range desired {
		found := false
		for _, raw := range liveRoutes {
			route, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			policy, _ := route["policy"].(string)
			if policy == "" {
				policy = "policyDst"
			}
			cidr, _ := route["cidr"].(string)
			hop, _ := route["nextHopIP"].(string)
			if cidr == want.CIDR && hop == want.NextHopIP && policy == want.Policy {
				found = true
				break
			}
		}
		if found {
			continue
		}
		out = append(out, map[string]any{
			"cidr": want.CIDR, "nextHopIP": want.NextHopIP, "policy": want.Policy,
		})
		changed = true
	}
	return out, changed
}

// MergeStrings is the same rule for the attachment array: add what is declared,
// disturb nothing else. The egress-gateway attach path appends to this list
// too.
func MergeStrings(liveValues []string, desired []string) ([]string, bool) {
	out := append([]string(nil), liveValues...)
	present := map[string]bool{}
	for _, v := range liveValues {
		present[v] = true
	}
	changed := false
	for _, want := range desired {
		if present[want] {
			continue
		}
		present[want] = true
		out = append(out, want)
		changed = true
	}
	return out, changed
}

// VPCSpec is the scalar part of the router — the fields that are simply set.
//
// The list-valued fields are merged instead, by the two helpers above, because
// this controller is not yet the only thing writing them.
func VPCSpec(net *platformv1alpha1.ManagedNetwork) map[string]any {
	spec := map[string]any{}
	if len(net.Spec.Namespaces) > 0 {
		// Written only when there is something to write. kube-ovn drops an
		// empty list rather than storing it, so rendering `[]` unconditionally
		// means the live object never matches the render and every pass issues
		// an update the API server normalises straight back — a write loop
		// invisible at the object level.
		spec["namespaces"] = toAny(net.Spec.Namespaces)
	}
	// The flag and the array travel together, always: each alone does nothing,
	// and the only way to be sure neither is written half-way is for one branch
	// to decide both.
	if len(Attachments(net)) > 0 || net.Spec.NATGateway {
		spec["enableExternal"] = true
	}
	return spec
}

// SubnetSpec is the default subnet.
//
// `acls` is not here on purpose. That list has one writer, and this controller
// is not it until the composer can prove its render equals the live list.
func SubnetSpec(net *platformv1alpha1.ManagedNetwork, gateway, dnsServer string) map[string]any {
	spec := map[string]any{
		"protocol":  "IPv4",
		"cidrBlock": net.Spec.CIDR,
		"gateway":   gateway,
		"vpc":       net.Name,
		// Marks this as the VPC's default: a namespace joining the VPC without
		// an explicit logical-switch annotation lands here. Without it, tenants
		// bound to the VPC land on the cluster overlay instead.
		"default":     true,
		"enableDHCP":  true,
		"natOutgoing": net.Spec.NATGateway,
	}
	if options := DHCPOptions(gateway, dnsServer); options != "" {
		spec["dhcpV4Options"] = options
	}
	if len(net.Spec.Namespaces) > 0 {
		spec["namespaces"] = toAny(net.Spec.Namespaces)
	}
	return spec
}

// DHCPOptions is what kube-ovn hands workloads over DHCP.
//
// The resolver is included only when one was declared. Defaulting it to
// something plausible would be worse than leaving it out: a wrong resolver
// address behaves exactly like a working one until something tries to resolve.
func DHCPOptions(gateway, dnsServer string) string {
	parts := []string{
		"lease_time=3600",
		"router=" + gateway,
		"server_id=" + gateway,
	}
	if dnsServer != "" {
		parts = append(parts, "dns_server="+dnsServer)
	}
	return strings.Join(parts, ",")
}

func toAny(in []string) []any {
	out := make([]any, 0, len(in))
	for _, v := range in {
		out = append(out, v)
	}
	return out
}

// Withdraw removes what this operator put here and no longer declares.
//
// The counterpart to MergeStrings, and it needs a third input for a reason:
// "live minus wanted" would delete another writer's work, which is exactly what
// the merge exists to protect. What was applied on the last pass is the record
// of what is ours — anything else on the list was put there by somebody, and
// not being asked for is not the same as being ours to remove.
//
// So an entry goes only when it was ours and is no longer wanted. A network
// adopted from the product has an empty record, so nothing of its is touched
// until this operator has written it once itself.
func Withdraw(live, wanted, applied []string) ([]string, bool) {
	wantedSet := map[string]bool{}
	for _, name := range wanted {
		wantedSet[name] = true
	}
	appliedSet := map[string]bool{}
	for _, name := range applied {
		appliedSet[name] = true
	}

	out := make([]string, 0, len(live))
	changed := false
	for _, name := range live {
		if appliedSet[name] && !wantedSet[name] {
			changed = true
			continue
		}
		out = append(out, name)
	}
	return out, changed
}

// WithdrawRoute removes a default route this operator put here and no longer
// declares. Matched on the next hop it was written with, so a default route
// somebody else wrote — through a different gateway — is left where it is.
func WithdrawRoute(liveRoutes []any, appliedNextHop string) ([]any, bool) {
	if appliedNextHop == "" {
		return liveRoutes, false
	}
	out := make([]any, 0, len(liveRoutes))
	changed := false
	for _, raw := range liveRoutes {
		route, _ := raw.(map[string]any)
		if route != nil && route["cidr"] == "0.0.0.0/0" &&
			route["nextHopIP"] == appliedNextHop {
			changed = true
			continue
		}
		out = append(out, raw)
	}
	return out, changed
}
