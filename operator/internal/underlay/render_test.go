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

package underlay

import (
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

func underlayFor(iface string) *platformv1alpha1.ManagedUnderlay {
	return &platformv1alpha1.ManagedUnderlay{
		ObjectMeta: metav1.ObjectMeta{Name: "u"},
		Spec: platformv1alpha1.ManagedUnderlaySpec{
			Interface:       iface,
			ExternalCIDR:    "10.60.0.0/24",
			ExternalGateway: "10.60.0.1",
		},
	}
}

// TestDefaultsMatchTheWizard: a CR written with nothing but the addressing must
// render the objects the wizard used to build, or an install that migrates gets
// a second, differently named fabric beside the first.
func TestDefaultsMatchTheWizard(t *testing.T) {
	u := underlayFor("eth1")
	if got := ProviderNetworkName(u); got != "external" {
		t.Errorf("provider network = %q", got)
	}
	if got := VLANName(u); got != "vlan-external" {
		t.Errorf("vlan = %q", got)
	}
	if got := SubnetName(u); got != "ext-sub" {
		t.Errorf("subnet = %q", got)
	}
	if got := Provider(u, "kube-system"); got != "ext-sub.kube-system.ovn" {
		t.Errorf("provider string = %q", got)
	}
}

// TestWatchedInterfacesIsTheWholeCluster: one DaemonSet watches every provider
// NIC. A per-underlay script is how the second underlay disowned the first
// one's NIC and took the control-plane transit down with it.
func TestWatchedInterfacesIsTheWholeCluster(t *testing.T) {
	got := WatchedInterfaces("eth0.310", []string{"eth0.300", "eth0.310", "", " eth0.320 "})
	want := []string{"eth0.300", "eth0.310", "eth0.320"}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
}

// TestWatchedInterfacesIsStable: the list is assembled from objects whose
// listing order the API server does not promise. An unstable order would
// rewrite the DaemonSet — and restart every watcher pod — on a pass where
// nothing changed.
func TestWatchedInterfacesIsStable(t *testing.T) {
	a := LinkWatcherScript(WatchedInterfaces("eth0.300", []string{"eth0.320", "eth0.310"}))
	b := LinkWatcherScript(WatchedInterfaces("eth0.300", []string{"eth0.310", "eth0.320"}))
	if a != b {
		t.Fatalf("script depends on listing order:\n%s\n---\n%s", a, b)
	}
}

// TestWatchedInterfacesSurvivesAnEmptyCluster guards the boundary: no other
// provider network, and nothing to watch but our own NIC.
func TestWatchedInterfacesSurvivesAnEmptyCluster(t *testing.T) {
	if got := WatchedInterfaces("eth1", nil); len(got) != 1 || got[0] != "eth1" {
		t.Fatalf("got %v", got)
	}
	if got := WatchedInterfaces("", nil); len(got) != 0 {
		t.Fatalf("got %v", got)
	}
}

// TestLinkWatcherImageFallsBackInOrder: the CR wins, then kube-ovn's own image
// (already pulled on exactly these nodes, and it carries iproute2), then a
// public one. The public default is last because a public default is a
// dependency that can rot — one stopped serving a layer and the watcher sat in
// ImagePullBackOff at desired 3, ready 0.
func TestLinkWatcherImageFallsBackInOrder(t *testing.T) {
	u := underlayFor("eth1")
	if got := LinkWatcher(u, "kube-system", "kubeovn/kube-ovn:v1", nil).
		Spec.Template.Spec.Containers[0].Image; got != "kubeovn/kube-ovn:v1" {
		t.Errorf("discovered image not used: %q", got)
	}
	if got := LinkWatcher(u, "kube-system", "", nil).
		Spec.Template.Spec.Containers[0].Image; got != FallbackWatcherImage {
		t.Errorf("fallback not used: %q", got)
	}
	u.Spec.LinkWatcherImage = "example.invalid/mine:1"
	if got := LinkWatcher(u, "kube-system", "kubeovn/kube-ovn:v1", nil).
		Spec.Template.Spec.Containers[0].Image; got != "example.invalid/mine:1" {
		t.Errorf("explicit image not honoured: %q", got)
	}
}

// TestWorkaroundsSayWhyAndWhen: the reason is prose and lives in an annotation.
// It was a label value once — the API server refuses those, no spaces and 63
// characters — so both DaemonSets came back 422 while the four fabric objects
// were created, and the underlay looked built with no link watcher behind it.
func TestWorkaroundsSayWhyAndWhen(t *testing.T) {
	u := underlayFor("eth1")
	for _, ds := range []struct {
		what string
		obj  interface {
			GetLabels() map[string]string
			GetAnnotations() map[string]string
		}
	}{
		{"link watcher", LinkWatcher(u, "kube-system", "img", []string{"eth1"})},
		{"cilium exempt", CiliumExempt(u, "kube-system")},
	} {
		if ds.obj.GetLabels()[WorkaroundLabel] != "true" {
			t.Errorf("%s: not marked as a workaround", ds.what)
		}
		for _, key := range []string{WorkaroundReason, WorkaroundRemoveWhen} {
			value := ds.obj.GetAnnotations()[key]
			if value == "" {
				t.Errorf("%s: %s is empty", ds.what, key)
			}
			// The thing that made these 422: a label value cannot hold a
			// sentence. Asserting the sentence is a sentence is the point.
			if !strings.Contains(value, " ") {
				t.Errorf("%s: %s = %q is not prose", ds.what, key, value)
			}
		}
	}
}

// TestLinkWatcherRunsOnlyOnGatewayNodes: the DaemonSet selects on the label the
// controller heals. Losing this selector would put a privileged pod on every
// node in the cluster.
func TestLinkWatcherRunsOnlyOnGatewayNodes(t *testing.T) {
	ds := LinkWatcher(underlayFor("eth1"), "kube-system", "img", []string{"eth1"})
	if got := ds.Spec.Template.Spec.NodeSelector[ExternalGWLabel]; got != "true" {
		t.Fatalf("nodeSelector[%s] = %q", ExternalGWLabel, got)
	}
	if !ds.Spec.Template.Spec.HostNetwork {
		t.Error("the watcher raises a host NIC and must be on the host network")
	}
	if c := ds.Spec.Template.Spec.Containers[0]; c.SecurityContext == nil ||
		c.SecurityContext.Privileged == nil || !*c.SecurityContext.Privileged {
		t.Error("`ip link set up` needs privileges it would not otherwise have")
	}
}

// TestExternalSubnetDoesNotNAT: the gateways SNAT (or route) for themselves. An
// underlay doing it too rewrites the source address the border is meant to see.
func TestExternalSubnetDoesNotNAT(t *testing.T) {
	subnet := ExternalSubnet(underlayFor("eth1"), "kube-system")
	spec, _ := subnet.Object["spec"].(map[string]any)
	if spec["natOutgoing"] != false {
		t.Errorf("natOutgoing = %v", spec["natOutgoing"])
	}
	// The upstream gateway is somebody else's kit and need not answer a ping;
	// a failed check blocks the whole subnet.
	if spec["disableGatewayCheck"] != true {
		t.Errorf("disableGatewayCheck = %v", spec["disableGatewayCheck"])
	}
	if subnet.GetLabels()[InfraPurposeLabel] != "infrastructure" {
		t.Errorf("the rest of the UI finds this subnet by label: %v", subnet.GetLabels())
	}
}
