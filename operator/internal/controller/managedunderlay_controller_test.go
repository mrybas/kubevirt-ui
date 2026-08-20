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
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/underlay"
)

func mustUnderlay(t *testing.T, u *platformv1alpha1.ManagedUnderlay) *platformv1alpha1.ManagedUnderlay {
	t.Helper()
	if err := k8sClient.Create(testCtx, u); err != nil {
		t.Fatalf("creating underlay %s: %v", u.Name, err)
	}
	t.Cleanup(func() {
		_ = k8sClient.Delete(testCtx, u)
	})
	return u
}

func getUnderlay(t *testing.T, name string) *platformv1alpha1.ManagedUnderlay {
	t.Helper()
	out := &platformv1alpha1.ManagedUnderlay{}
	// Retried: the manager's cache lags a write by a beat, and a bare Get right
	// after a create is the flakiest line in this suite.
	var last error
	for i := 0; i < 50; i++ {
		last = k8sClient.Get(testCtx, types.NamespacedName{Name: name}, out)
		if last == nil {
			return out
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("reading underlay %s: %v", name, last)
	return nil
}

// mustKubeOVNNamespace stands in for the cluster's kube-ovn install: the
// controller finds the namespace by looking for kube-ovn's own CNI DaemonSet,
// so the test has to put one somewhere.
func mustKubeOVNNamespace(t *testing.T, namespace, image string) {
	t.Helper()
	mustNamespace(t, namespace, "")
	ds := &unstructured.Unstructured{}
	ds.SetAPIVersion("apps/v1")
	ds.SetKind("DaemonSet")
	ds.SetName(underlay.KubeOVNCNIDaemonSet)
	ds.SetNamespace(namespace)
	_ = unstructured.SetNestedMap(ds.Object, map[string]any{
		"selector": map[string]any{
			"matchLabels": map[string]any{"app": "kube-ovn-cni"},
		},
		"template": map[string]any{
			"metadata": map[string]any{
				"labels": map[string]any{"app": "kube-ovn-cni"},
			},
			"spec": map[string]any{
				"containers": []any{map[string]any{"name": "cni-server", "image": image}},
			},
		},
	}, "spec")
	if err := k8sClient.Create(testCtx, ds); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the kube-ovn CNI DaemonSet: %v", err)
	}
}

// mustReadyNodes is kube-ovn's answer to "which nodes carry this NIC".
func mustReadyNodes(t *testing.T, providerNetwork string, nodes ...string) {
	t.Helper()
	pn := &unstructured.Unstructured{}
	pn.SetGroupVersionKind(providerNetworkGVK)
	eventually(t, "the provider network to exist", func() error {
		return k8sClient.Get(testCtx, types.NamespacedName{Name: providerNetwork}, pn)
	})
	values := make([]any, 0, len(nodes))
	for _, n := range nodes {
		values = append(values, n)
	}
	_ = unstructured.SetNestedMap(pn.Object, map[string]any{"readyNodes": values}, "status")
	if err := k8sClient.Status().Update(testCtx, pn); err != nil {
		t.Fatalf("setting readyNodes on %s: %v", providerNetwork, err)
	}
}

func mustNode(t *testing.T, name string) {
	t.Helper()
	node := &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: name}}
	if err := k8sClient.Create(testCtx, node); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating node %s: %v", name, err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, node) })
}

func underlayCondition(u *platformv1alpha1.ManagedUnderlay, kind string) *metav1.Condition {
	return apimeta.FindStatusCondition(u.Status.Conditions, kind)
}

// TestUnderlayBuildsTheFabric is the base case: four objects, in dependency
// order, each owned by the CR that asked for them.
func TestUnderlayBuildsTheFabric(t *testing.T) {
	mustKubeOVNNamespace(t, "kube-ovn-fabric", "kubeovn/kube-ovn:v1.14.0")

	mustUnderlay(t, &platformv1alpha1.ManagedUnderlay{
		ObjectMeta: metav1.ObjectMeta{Name: "fabric"},
		Spec: platformv1alpha1.ManagedUnderlaySpec{
			Interface:           "eth1",
			ExternalCIDR:        "10.60.0.0/24",
			ExternalGateway:     "10.60.0.1",
			VLANID:              310,
			ExcludeNodes:        []string{"cp-1"},
			ExcludeIPs:          []string{"10.60.0.1..10.60.0.10"},
			ProviderNetworkName: "fabric-pn",
			VLANName:            "fabric-vlan",
			SubnetName:          "fabric-sub",
			KubeOVNNamespace:    "kube-ovn-fabric",
		},
	})

	eventually(t, "the provider network", func() error {
		pn := &unstructured.Unstructured{}
		pn.SetGroupVersionKind(providerNetworkGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "fabric-pn"}, pn); err != nil {
			return err
		}
		iface, _, _ := unstructured.NestedString(pn.Object, "spec", "defaultInterface")
		if iface != "eth1" {
			return fmt.Errorf("defaultInterface = %q", iface)
		}
		excluded, _, _ := unstructured.NestedStringSlice(pn.Object, "spec", "excludeNodes")
		if len(excluded) != 1 || excluded[0] != "cp-1" {
			return fmt.Errorf("excludeNodes = %v", excluded)
		}
		if owners := pn.GetOwnerReferences(); len(owners) != 1 || owners[0].Name != "fabric" {
			return fmt.Errorf("ownerReferences = %v", owners)
		}
		return nil
	})

	eventually(t, "the vlan", func() error {
		vlan := &unstructured.Unstructured{}
		vlan.SetGroupVersionKind(vlanGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "fabric-vlan"}, vlan); err != nil {
			return err
		}
		id, _, _ := unstructured.NestedInt64(vlan.Object, "spec", "id")
		provider, _, _ := unstructured.NestedString(vlan.Object, "spec", "provider")
		if id != 310 || provider != "fabric-pn" {
			return fmt.Errorf("id=%d provider=%q", id, provider)
		}
		return nil
	})

	eventually(t, "the NAD", func() error {
		nad := &unstructured.Unstructured{}
		nad.SetGroupVersionKind(nadGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kube-ovn-fabric", Name: "fabric-sub",
		}, nad); err != nil {
			return err
		}
		raw, _, _ := unstructured.NestedString(nad.Object, "spec", "config")
		var config map[string]any
		if err := json.Unmarshal([]byte(raw), &config); err != nil {
			return fmt.Errorf("config is not JSON: %w", err)
		}
		// kube-ovn compares this string character for character and reports a
		// mismatch only by refusing to build the gateway.
		if config["provider"] != "fabric-sub.kube-ovn-fabric.ovn" {
			return fmt.Errorf("provider = %v", config["provider"])
		}
		return nil
	})

	eventually(t, "the external subnet", func() error {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "fabric-sub"}, subnet); err != nil {
			return err
		}
		spec, _, _ := unstructured.NestedMap(subnet.Object, "spec")
		if spec["cidrBlock"] != "10.60.0.0/24" || spec["gateway"] != "10.60.0.1" {
			return fmt.Errorf("addressing = %v / %v", spec["cidrBlock"], spec["gateway"])
		}
		if spec["vlan"] != "fabric-vlan" {
			return fmt.Errorf("vlan = %v", spec["vlan"])
		}
		// The gateways SNAT for themselves; the underlay doing it too would
		// rewrite the source address the border is supposed to see.
		if spec["natOutgoing"] != false {
			return fmt.Errorf("natOutgoing = %v", spec["natOutgoing"])
		}
		// The upstream gateway need not answer kube-ovn's ping, and a failed
		// check blocks the subnet outright.
		if spec["disableGatewayCheck"] != true {
			return fmt.Errorf("disableGatewayCheck = %v", spec["disableGatewayCheck"])
		}
		if subnet.GetLabels()[underlay.InfraPurposeLabel] != "infrastructure" {
			return fmt.Errorf("labels = %v", subnet.GetLabels())
		}
		return nil
	})

	eventually(t, "FabricReady", func() error {
		cond := underlayCondition(getUnderlay(t, "fabric"), platformv1alpha1.ConditionFabricReady)
		if cond == nil || cond.Status != metav1.ConditionTrue {
			return fmt.Errorf("condition = %v", cond)
		}
		return nil
	})
}

// TestUnderlayRefusesWithoutReadyNodes is the honest failure: kube-ovn says no
// node carries the NIC, so there is nothing to label and the message says where
// to look.
func TestUnderlayRefusesWithoutReadyNodes(t *testing.T) {
	mustKubeOVNNamespace(t, "kube-ovn-nonodes", "kubeovn/kube-ovn:v1.14.0")

	mustUnderlay(t, &platformv1alpha1.ManagedUnderlay{
		ObjectMeta: metav1.ObjectMeta{Name: "nonodes"},
		Spec: platformv1alpha1.ManagedUnderlaySpec{
			Interface:           "eth9",
			ExternalCIDR:        "10.61.0.0/24",
			ExternalGateway:     "10.61.0.1",
			ProviderNetworkName: "nonodes-pn",
			VLANName:            "nonodes-vlan",
			SubnetName:          "nonodes-sub",
			KubeOVNNamespace:    "kube-ovn-nonodes",
		},
	})

	eventually(t, "an honest refusal", func() error {
		u := getUnderlay(t, "nonodes")
		fabric := underlayCondition(u, platformv1alpha1.ConditionFabricReady)
		labelled := underlayCondition(u, platformv1alpha1.ConditionNodesLabelled)
		if fabric == nil || fabric.Status != metav1.ConditionTrue {
			return fmt.Errorf("fabric = %v", fabric)
		}
		if labelled == nil || labelled.Status != metav1.ConditionFalse {
			return fmt.Errorf("labelled = %v", labelled)
		}
		if labelled.Reason != "NoReadyNodes" {
			return fmt.Errorf("reason = %s", labelled.Reason)
		}
		// The point of the message is that it names the NIC and the object to
		// read next. A "not ready" with neither is the state this whole
		// controller exists to stop producing.
		if !strings.Contains(labelled.Message, "eth9") ||
			!strings.Contains(labelled.Message, "ProviderNetwork") {
			return fmt.Errorf("message = %q", labelled.Message)
		}
		return nil
	})
}

// TestUnderlayHealsTheGatewayLabel is the defect this controller was written
// for. The heal used to live on the GET: nothing put the label back unless
// somebody opened the page, and nothing reported it missing.
func TestUnderlayHealsTheGatewayLabel(t *testing.T) {
	mustKubeOVNNamespace(t, "kube-ovn-heal", "kubeovn/kube-ovn:v1.14.0")
	mustNode(t, "heal-worker-1")
	mustNode(t, "heal-worker-2")

	mustUnderlay(t, &platformv1alpha1.ManagedUnderlay{
		ObjectMeta: metav1.ObjectMeta{Name: "heal"},
		Spec: platformv1alpha1.ManagedUnderlaySpec{
			Interface:           "eth1",
			ExternalCIDR:        "10.62.0.0/24",
			ExternalGateway:     "10.62.0.1",
			ProviderNetworkName: "heal-pn",
			VLANName:            "heal-vlan",
			SubnetName:          "heal-sub",
			KubeOVNNamespace:    "kube-ovn-heal",
		},
	})

	mustReadyNodes(t, "heal-pn", "heal-worker-1", "heal-worker-2")

	eventually(t, "both nodes to carry the gateway label", func() error {
		for _, name := range []string{"heal-worker-1", "heal-worker-2"} {
			node := &corev1.Node{}
			if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, node); err != nil {
				return err
			}
			if node.Labels[underlay.ExternalGWLabel] != "true" {
				return fmt.Errorf("%s: %s = %q", name,
					underlay.ExternalGWLabel, node.Labels[underlay.ExternalGWLabel])
			}
		}
		return nil
	})

	healsBefore := getUnderlay(t, "heal").Status.LabelHeals

	// The exact shape it was found in on the lab: an explicit `false`, with
	// nothing in managedFields claiming it. Arguing about the author does not
	// bring the underlay back.
	node := &corev1.Node{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "heal-worker-1"}, node); err != nil {
		t.Fatalf("reading the node: %v", err)
	}
	patched := node.DeepCopy()
	patched.Labels[underlay.ExternalGWLabel] = "false"
	if err := k8sClient.Patch(testCtx, patched, client.MergeFrom(node)); err != nil {
		t.Fatalf("breaking the label: %v", err)
	}

	eventually(t, "the label to come back on its own", func() error {
		current := &corev1.Node{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "heal-worker-1"}, current); err != nil {
			return err
		}
		if current.Labels[underlay.ExternalGWLabel] != "true" {
			return fmt.Errorf("still %q", current.Labels[underlay.ExternalGWLabel])
		}
		return nil
	})

	// Counted, because a number that keeps climbing is the only evidence that
	// something else is still writing this label.
	eventually(t, "the heal to be counted", func() error {
		heals := getUnderlay(t, "heal").Status.LabelHeals
		if heals <= healsBefore {
			return fmt.Errorf("labelHeals = %d, was %d", heals, healsBefore)
		}
		return nil
	})
}

// TestUnderlayLeavesUnreadyNodesAlone: the heal is add-only. A node dropping out
// of readyNodes is far more often kube-ovn being briefly unable to report than
// the NIC being gone, and stripping the label then would take the link watcher
// down at exactly the wrong moment.
func TestUnderlayLeavesUnreadyNodesAlone(t *testing.T) {
	mustKubeOVNNamespace(t, "kube-ovn-addonly", "kubeovn/kube-ovn:v1.14.0")
	mustNode(t, "addonly-worker")

	// Somebody else's label, on a node this underlay does not claim.
	other := &corev1.Node{ObjectMeta: metav1.ObjectMeta{
		Name:   "addonly-outsider",
		Labels: map[string]string{underlay.ExternalGWLabel: "true"},
	}}
	if err := k8sClient.Create(testCtx, other); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the outsider node: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, other) })

	mustUnderlay(t, &platformv1alpha1.ManagedUnderlay{
		ObjectMeta: metav1.ObjectMeta{Name: "addonly"},
		Spec: platformv1alpha1.ManagedUnderlaySpec{
			Interface:           "eth1",
			ExternalCIDR:        "10.63.0.0/24",
			ExternalGateway:     "10.63.0.1",
			ProviderNetworkName: "addonly-pn",
			VLANName:            "addonly-vlan",
			SubnetName:          "addonly-sub",
			KubeOVNNamespace:    "kube-ovn-addonly",
		},
	})
	mustReadyNodes(t, "addonly-pn", "addonly-worker")

	eventually(t, "the claimed node to be labelled", func() error {
		node := &corev1.Node{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "addonly-worker"}, node); err != nil {
			return err
		}
		if node.Labels[underlay.ExternalGWLabel] != "true" {
			return fmt.Errorf("not labelled yet")
		}
		return nil
	})

	consistently(t, "the outsider's label untouched", 3*time.Second, func() error {
		node := &corev1.Node{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "addonly-outsider"}, node); err != nil {
			return err
		}
		if node.Labels[underlay.ExternalGWLabel] != "true" {
			return fmt.Errorf("stripped a label it does not own")
		}
		return nil
	})
}

// TestUnderlayReportsADaemonSetDoingNothing: a DaemonSet scheduled on no node
// reads as healthy everywhere else. `kubectl rollout status` says "successfully
// rolled out", because zero desired pods are all ready.
func TestUnderlayReportsADaemonSetDoingNothing(t *testing.T) {
	mustKubeOVNNamespace(t, "kube-ovn-ds", "kubeovn/kube-ovn:v1.14.0")

	mustUnderlay(t, &platformv1alpha1.ManagedUnderlay{
		ObjectMeta: metav1.ObjectMeta{Name: "dsstate"},
		Spec: platformv1alpha1.ManagedUnderlaySpec{
			Interface:           "eth1",
			ExternalCIDR:        "10.64.0.0/24",
			ExternalGateway:     "10.64.0.1",
			ProviderNetworkName: "dsstate-pn",
			VLANName:            "dsstate-vlan",
			SubnetName:          "dsstate-sub",
			KubeOVNNamespace:    "kube-ovn-ds",
		},
	})

	eventually(t, "the link watcher and its verdict", func() error {
		u := getUnderlay(t, "dsstate")
		var watcher *platformv1alpha1.UnderlayDaemonSetStatus
		for i := range u.Status.DaemonSets {
			if u.Status.DaemonSets[i].Name == underlay.LinkWatcherName {
				watcher = &u.Status.DaemonSets[i]
			}
		}
		if watcher == nil {
			return fmt.Errorf("no link watcher in status: %v", u.Status.DaemonSets)
		}
		// There is no kubelet in envtest, so nothing is ever scheduled — which
		// is exactly the state being asserted.
		if watcher.State != "scheduled-nowhere" {
			return fmt.Errorf("state = %q", watcher.State)
		}
		running := underlayCondition(u, platformv1alpha1.ConditionWorkaroundsRunning)
		if running == nil || running.Status != metav1.ConditionFalse {
			return fmt.Errorf("WorkaroundsRunning = %v", running)
		}
		return nil
	})

	// The image is kube-ovn's own, because it is already pulled on exactly the
	// nodes the watcher runs on and it carries iproute2. A named public image
	// is a dependency that can rot.
	eventually(t, "the watcher to run kube-ovn's image", func() error {
		ds := &unstructured.Unstructured{}
		ds.SetAPIVersion("apps/v1")
		ds.SetKind("DaemonSet")
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kube-ovn-ds", Name: underlay.LinkWatcherName,
		}, ds); err != nil {
			return err
		}
		containers, _, _ := unstructured.NestedSlice(ds.Object, "spec", "template", "spec", "containers")
		if len(containers) != 1 {
			return fmt.Errorf("containers = %v", containers)
		}
		image, _ := containers[0].(map[string]any)["image"].(string)
		if image != "kubeovn/kube-ovn:v1.14.0" {
			return fmt.Errorf("image = %q", image)
		}
		if ds.GetAnnotations()[underlay.WorkaroundRemoveWhen] == "" {
			return fmt.Errorf("no removal condition recorded on a workaround")
		}
		return nil
	})
}

// TestUnderlayWatcherCoversEveryProviderNIC: there is one link-watcher
// DaemonSet per cluster. A per-underlay script meant the second underlay
// silently disowned the first one's NIC — which is how eth0.300 went down on
// two workers and took the control-plane transit with it.
func TestUnderlayWatcherCoversEveryProviderNIC(t *testing.T) {
	mustKubeOVNNamespace(t, "kube-ovn-pair", "kubeovn/kube-ovn:v1.14.0")

	for _, spec := range []struct{ name, iface, pn string }{
		{"pair-transit", "eth0.300", "pair-transit-pn"},
		{"pair-egress", "eth0.310", "pair-egress-pn"},
	} {
		mustUnderlay(t, &platformv1alpha1.ManagedUnderlay{
			ObjectMeta: metav1.ObjectMeta{Name: spec.name},
			Spec: platformv1alpha1.ManagedUnderlaySpec{
				Interface:           spec.iface,
				ExternalCIDR:        "10.65.0.0/24",
				ExternalGateway:     "10.65.0.1",
				ProviderNetworkName: spec.pn,
				VLANName:            spec.name + "-vlan",
				SubnetName:          spec.name + "-sub",
				KubeOVNNamespace:    "kube-ovn-pair",
			},
		})
	}

	eventually(t, "one watcher covering both NICs", func() error {
		ds := &unstructured.Unstructured{}
		ds.SetAPIVersion("apps/v1")
		ds.SetKind("DaemonSet")
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kube-ovn-pair", Name: underlay.LinkWatcherName,
		}, ds); err != nil {
			return err
		}
		containers, _, _ := unstructured.NestedSlice(ds.Object, "spec", "template", "spec", "containers")
		if len(containers) != 1 {
			return fmt.Errorf("containers = %v", containers)
		}
		args, _ := containers[0].(map[string]any)["args"].([]any)
		if len(args) != 1 {
			return fmt.Errorf("args = %v", args)
		}
		script, _ := args[0].(string)
		for _, iface := range []string{"eth0.300", "eth0.310"} {
			if !strings.Contains(script, iface) {
				return fmt.Errorf("%s is not watched:\n%s", iface, script)
			}
		}
		// Both underlays want the same singleton, so both own it and the
		// garbage collector removes it only when the last one is gone.
		owners := ds.GetOwnerReferences()
		if len(owners) != 2 {
			return fmt.Errorf("ownerReferences = %v", owners)
		}
		for _, owner := range owners {
			if owner.Controller != nil && *owner.Controller {
				return fmt.Errorf("%s claims to be the controller of a shared object", owner.Name)
			}
		}
		return nil
	})
}
