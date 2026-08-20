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

package network

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// live is a VPC or Subnet as it exists on the stand, captured from the objects
// the UI built. The point of the fixtures is that they are not written by hand:
// the thing being checked is "does the operator produce what the product
// already produces", and a hand-written expectation would only prove the
// renderer agrees with my memory of it.
type live struct {
	Labels      map[string]string `json:"labels"`
	Annotations map[string]string `json:"annotations"`
	Spec        map[string]any    `json:"spec"`
}

func loadLive(t *testing.T, name string) live {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("reading %s: %v", name, err)
	}
	var out live
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("parsing %s: %v", name, err)
	}
	if len(out.Spec) == 0 {
		t.Fatalf("%s has no spec — the fixture is empty and would prove nothing", name)
	}
	return out
}

// assertRenderedKeysMatch compares only what the renderer claims to own.
//
// kube-ovn writes its own defaults into the specs of its own objects
// (`enableLb`, `gatewayNode`, `private`, `provider`, `u2oFeatures`, empty
// `policyRoutes`/`vpcPeerings`, and an `excludeIps` derived from the gateway).
// Demanding equality over the whole spec would fail on fields nothing here
// writes, and merging over them would be wrong. So the comparison is: for every
// key the renderer produces, the live object agrees.
func assertRenderedKeysMatch(t *testing.T, what string, rendered, liveSpec map[string]any) {
	t.Helper()
	keys := make([]string, 0, len(rendered))
	for k := range rendered {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, key := range keys {
		got, present := liveSpec[key]
		if !present {
			t.Errorf("%s: the renderer writes %q and the live object has no such key", what, key)
			continue
		}
		if !equalJSON(rendered[key], got) {
			t.Errorf("%s: %s\n  rendered: %s\n  live:     %s",
				what, key, mustJSON(rendered[key]), mustJSON(got))
		}
	}
}

func equalJSON(a, b any) bool {
	var x, y any
	_ = json.Unmarshal([]byte(mustJSON(a)), &x)
	_ = json.Unmarshal([]byte(mustJSON(b)), &y)
	return reflect.DeepEqual(x, y)
}

func mustJSON(v any) string {
	out, err := json.Marshal(v)
	if err != nil {
		return "<unmarshalable>"
	}
	return string(out)
}

// tenantNetwork is the CR for a network the UI already built on the stand.
func tenantNetwork(name, cidr string) *platformv1alpha1.ManagedNetwork {
	return &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:        cidr,
			Folder:      "poc-transit",
			Environment: "dev",
			DNSServer:   "10.96.0.200",
			ExternalPlane: &platformv1alpha1.ExternalPlane{
				Attachments:  []string{"cp-transit", "external"},
				EgressSubnet: "external",
			},
		},
	}
}

// TestTheRenderMatchesWhatTheProductBuilt is the acceptance for this slice: the
// same declaration must produce the objects the UI is producing today, on the
// two networks the stand actually has.
func TestTheRenderMatchesWhatTheProductBuilt(t *testing.T) {
	for _, tc := range []struct {
		name, cidr, vpcFile, subnetFile string
	}{
		{"uat-net-vm", "10.200.0.0/22",
			"live-vpc-uat-net-vm.json", "live-subnet-uat-net-vm-default.json"},
		{"uat-net-t1", "10.200.4.0/22",
			"live-vpc-uat-net-t1.json", "live-subnet-uat-net-t1-default.json"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			net := tenantNetwork(tc.name, tc.cidr)
			gateway, err := Gateway(net)
			if err != nil {
				t.Fatalf("gateway: %v", err)
			}

			liveVPC := loadLive(t, tc.vpcFile)
			liveSubnet := loadLive(t, tc.subnetFile)

			// The next hop is read from the egress subnet at runtime; here it
			// is taken from the object the live VPC actually points at, which
			// is the same fact from the same place.
			nextHop := "10.199.4.254"

			assertRenderedKeysMatch(t, "Vpc", VPCSpec(net), liveVPC.Spec)
			assertRenderedKeysMatch(t, "Subnet", SubnetSpec(net, gateway, net.Spec.DNSServer), liveSubnet.Spec)

			// The list-valued fields are merged, so the acceptance is sharper
			// than equality: adopting a network the product already built must
			// change nothing at all.
			liveRoutes, _ := liveVPC.Spec["staticRoutes"].([]any)
			if _, changed := MergeRoutes(liveRoutes, DesiredRoutes(net, nextHop)); changed {
				t.Errorf("adopting %s would rewrite staticRoutes; live: %s",
					tc.name, mustJSON(liveRoutes))
			}
			var liveAttached []string
			for _, v := range liveVPC.Spec["extraExternalSubnets"].([]any) {
				liveAttached = append(liveAttached, v.(string))
			}
			if _, changed := MergeStrings(liveAttached, Attachments(net)); changed {
				t.Errorf("adopting %s would rewrite extraExternalSubnets; live: %v",
					tc.name, liveAttached)
			}

			if !reflect.DeepEqual(Labels(net), liveVPC.Labels) {
				t.Errorf("Vpc labels:\n  rendered: %v\n  live:     %v", Labels(net), liveVPC.Labels)
			}
			if !reflect.DeepEqual(Labels(net), liveSubnet.Labels) {
				t.Errorf("Subnet labels:\n  rendered: %v\n  live:     %v", Labels(net), liveSubnet.Labels)
			}
			// These networks were created isolated, so no opt-out was recorded.
			// Absence is the signal, so absence is what is checked.
			if _, present := liveSubnet.Annotations[IsolationOptOutAnnotation]; present {
				t.Errorf("the fixture carries an opt-out; the CR under test does not")
			}
		})
	}
}

// TestAnEmptyListIsNotWritten guards the shape of the write, not its content.
//
// kube-ovn drops an empty `namespaces` rather than storing it, so rendering
// `[]` unconditionally means the live object never equals the render: every
// pass issues an update the API server normalises straight back. That is a
// write loop with no visible symptom — resourceVersion does not even move.
func TestAnEmptyListIsNotWritten(t *testing.T) {
	spec := VPCSpec(tenantNetwork("x", "10.200.0.0/22"))
	if _, present := spec["namespaces"]; present {
		t.Errorf("namespaces written for an empty list: %v", spec["namespaces"])
	}
	subnet := SubnetSpec(tenantNetwork("x", "10.200.0.0/22"), "10.200.0.1", "10.96.0.200")
	if _, present := subnet["namespaces"]; present {
		t.Errorf("namespaces written for an empty list: %v", subnet["namespaces"])
	}
}

// TestTheFlagAndTheArrayTravelTogether: each alone does nothing. A VPC with the
// flag and an empty array had no external port; a VPC with the array and no
// flag had no ports at all. Both measured, a day apart.
func TestTheFlagAndTheArrayTravelTogether(t *testing.T) {
	with := VPCSpec(tenantNetwork("x", "10.200.0.0/22"))
	if with["enableExternal"] != true {
		t.Error("attachments declared without the master switch")
	}

	bare := &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "bare"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.16.0/22"},
	}
	without := VPCSpec(bare)
	if _, present := without["enableExternal"]; present {
		t.Error("master switch written with nothing to attach")
	}
	if len(Attachments(bare)) != 0 {
		t.Error("attachments produced for a network that declared none")
	}
	if got := DesiredRoutes(bare, ""); len(got) != 0 {
		t.Errorf("a default route produced with no egress subnet declared: %v", got)
	}
}

// TestTheEgressSubnetIsAlwaysAttached: a default route into a subnet the VPC
// has no port on is a route to nowhere, so the two halves are one decision.
func TestTheEgressSubnetIsAlwaysAttached(t *testing.T) {
	net := &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "x"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR: "10.200.20.0/22",
			ExternalPlane: &platformv1alpha1.ExternalPlane{
				Attachments:  []string{"cp-transit"},
				EgressSubnet: "external",
			},
		},
	}
	got := Attachments(net)
	want := []string{"cp-transit", "external"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	// And listing it twice does not attach it twice.
	net.Spec.ExternalPlane.Attachments = []string{"cp-transit", "external"}
	if got := Attachments(net); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

// TestTheResolverIsOnlyPromisedWhenDeclared: a wrong resolver address behaves
// exactly like a working one until something tries to resolve, so a plausible
// default is worse than none.
func TestTheResolverIsOnlyPromisedWhenDeclared(t *testing.T) {
	if got := DHCPOptions("10.200.0.1", "10.96.0.200"); got !=
		"lease_time=3600,router=10.200.0.1,server_id=10.200.0.1,dns_server=10.96.0.200" {
		t.Errorf("got %q", got)
	}
	if got := DHCPOptions("10.200.0.1", ""); got !=
		"lease_time=3600,router=10.200.0.1,server_id=10.200.0.1" {
		t.Errorf("got %q", got)
	}
}

// TestIsolationDefaultsToClosed: the old default ran the other way, and silence
// read as consent to stay open.
func TestIsolationDefaultsToClosed(t *testing.T) {
	net := tenantNetwork("x", "10.200.0.0/22")
	if !IsIsolated(net) {
		t.Error("a network whose isolation was never stated must be isolated")
	}
	no := false
	net.Spec.Isolated = &no
	if IsIsolated(net) {
		t.Error("an explicit no must be honoured")
	}
}
