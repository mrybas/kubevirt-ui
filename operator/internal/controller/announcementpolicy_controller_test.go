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
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/metrics"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func mustSubnetWithVPC(t *testing.T, name, vpc, cidr string) {
	t.Helper()
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	subnet.SetName(name)
	spec := map[string]any{"cidrBlock": cidr, "protocol": "IPv4"}
	if vpc != "" {
		spec["vpc"] = vpc
	}
	if err := unstructured.SetNestedMap(subnet.Object, spec, "spec"); err != nil {
		t.Fatalf("building subnet: %v", err)
	}
	if err := k8sClient.Create(testCtx, subnet); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating subnet %s: %v", name, err)
	}
}

func mustVPCWithDefaultRoute(t *testing.T, name, nextHop string) {
	t.Helper()
	vpc := &unstructured.Unstructured{}
	vpc.SetGroupVersionKind(vpcGVK)
	vpc.SetName(name)
	spec := map[string]any{}
	if nextHop != "" {
		spec["staticRoutes"] = []any{map[string]any{
			"cidr": "0.0.0.0/0", "nextHopIP": nextHop, "policy": "policyDst",
		}}
	}
	if err := unstructured.SetNestedMap(vpc.Object, spec, "spec"); err != nil {
		t.Fatalf("building vpc: %v", err)
	}
	if err := k8sClient.Create(testCtx, vpc); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating vpc %s: %v", name, err)
	}
}

func mustRouterLeg(t *testing.T, name, externalSubnet, address string) {
	t.Helper()
	eip := &unstructured.Unstructured{}
	eip.SetGroupVersionKind(ovnEipGVK)
	eip.SetName(name)
	if err := unstructured.SetNestedMap(eip.Object, map[string]any{
		"type": "lrp", "externalSubnet": externalSubnet,
	}, "spec"); err != nil {
		t.Fatalf("building router leg: %v", err)
	}
	if err := k8sClient.Create(testCtx, eip); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating router leg %s: %v", name, err)
	}
	eventually(t, "the router leg to report its address", func() error {
		got := &unstructured.Unstructured{}
		got.SetGroupVersionKind(ovnEipGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, got); err != nil {
			return err
		}
		if err := unstructured.SetNestedField(got.Object, address, "status", "v4Ip"); err != nil {
			return err
		}
		return k8sClient.Status().Update(testCtx, got)
	})
}

func mustWorkerNode(t *testing.T, name string, controlPlane bool) {
	t.Helper()
	node := &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: name, Labels: map[string]string{}}}
	if controlPlane {
		node.Labels["node-role.kubernetes.io/control-plane"] = ""
	}
	if err := k8sClient.Create(testCtx, node); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating node %s: %v", name, err)
	}
	eventually(t, "the node to be Ready", func() error {
		got := &corev1.Node{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, got); err != nil {
			return err
		}
		got.Status.Conditions = []corev1.NodeCondition{{
			Type: corev1.NodeReady, Status: corev1.ConditionTrue,
		}}
		return k8sClient.Status().Update(testCtx, got)
	})
}

func frrConfig(t *testing.T, namespace string) *unstructured.Unstructured {
	t.Helper()
	cfg := &unstructured.Unstructured{}
	cfg.SetGroupVersionKind(frrConfigGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: namespace, Name: defaultFRRConfigName,
	}, cfg); err != nil {
		t.Fatalf("reading the generated configuration: %v", err)
	}
	return cfg
}

func rawConfigOf(t *testing.T, cfg *unstructured.Unstructured) string {
	t.Helper()
	raw, _, _ := unstructured.NestedString(cfg.Object, "spec", "raw", "rawConfig")
	return raw
}

// A leg on the external plane is not enough. The first run of the generator
// this replaces offered six prefixes, four of which belonged to networks whose
// traffic left somewhere else entirely — putting a second, competing path to
// each of those prefixes on the border.
func TestOnlyNetworksRoutedThroughTheExternalPlaneAreAdvertised(t *testing.T) {
	ns := "frr-scope"
	mustNamespace(t, ns, "")
	mustWorkerNode(t, "ann-worker-1", false)
	mustWorkerNode(t, "ann-cp-1", true)

	mustSubnetWithVPC(t, "external", "ovn-cluster", "10.199.4.0/22")

	// Routed: its default route points into the external plane.
	mustVPCWithDefaultRoute(t, "routed-vpc", "10.199.4.254")
	mustSubnetWithVPC(t, "routed-vpc-default", "routed-vpc", "10.200.0.0/22")
	mustRouterLeg(t, "routed-vpc-external", "external", "10.199.4.1")

	// Not routed: it has a leg, but its traffic leaves through a gateway.
	mustVPCWithDefaultRoute(t, "hub-vpc", "10.199.0.5")
	mustSubnetWithVPC(t, "hub-vpc-default", "hub-vpc", "10.200.8.0/22")
	mustRouterLeg(t, "hub-vpc-external", "external", "10.199.4.9")

	policy := &platformv1alpha1.AnnouncementPolicy{
		ObjectMeta: metav1.ObjectMeta{Name: "default"},
		Spec: platformv1alpha1.AnnouncementPolicySpec{
			BorderPeer:      "10.198.175.254",
			LocalASN:        65030,
			PeerASN:         65000,
			ExternalSubnet:  "external",
			TargetNamespace: ns,
			Replicas:        2,
		},
	}
	if err := k8sClient.Create(testCtx, policy); err != nil {
		t.Fatalf("creating policy: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, policy) })

	eventually(t, "only the routed network to be advertised", func() error {
		got := &platformv1alpha1.AnnouncementPolicy{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "default"}, got); err != nil {
			return err
		}
		if len(got.Status.Announced) != 1 {
			return fmt.Errorf("announced = %+v, want just the routed network", got.Status.Announced)
		}
		a := got.Status.Announced[0]
		if a.VPC != "routed-vpc" || a.CIDR != "10.200.0.0/22" || a.NextHop != "10.199.4.1" {
			return fmt.Errorf("announced %+v", a)
		}
		return nil
	})

	// The control plane is not among the speakers: the border peers with
	// workers, and announcing from a node nothing listens to makes every
	// prefix vanish while this object looks perfect.
	eventually(t, "the announcement to come from workers only", func() error {
		got := &platformv1alpha1.AnnouncementPolicy{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "default"}, got); err != nil {
			return err
		}
		for _, node := range got.Status.Nodes {
			if strings.Contains(node, "cp-") {
				return fmt.Errorf("a control-plane node is carrying the announcement: %v", got.Status.Nodes)
			}
		}
		if len(got.Status.Nodes) == 0 {
			return fmt.Errorf("nobody is carrying the announcement")
		}
		return nil
	})

	raw := rawConfigOf(t, frrConfig(t, ns))
	if !strings.Contains(raw, "network 10.200.0.0/22") {
		t.Fatalf("the routed prefix is not in the configuration:\n%s", raw)
	}
	if strings.Contains(raw, "10.200.8.0/22") {
		t.Fatalf("a network that leaves through a gateway was advertised:\n%s", raw)
	}
}

// Every write reloads FRR, and a reload is the one moment a session can flap.
func TestAnUnchangedClusterProducesNoWrites(t *testing.T) {
	ns := "frr-quiet"
	mustNamespace(t, ns, "")
	mustWorkerNode(t, "quiet-worker-1", false)
	mustSubnetWithVPC(t, "external", "ovn-cluster", "10.199.4.0/22")

	policy := &platformv1alpha1.AnnouncementPolicy{
		ObjectMeta: metav1.ObjectMeta{Name: "default"},
		Spec: platformv1alpha1.AnnouncementPolicySpec{
			BorderPeer:      "10.198.175.254",
			LocalASN:        65030,
			PeerASN:         65000,
			TargetNamespace: ns,
		},
	}
	if err := k8sClient.Create(testCtx, policy); err != nil {
		t.Fatalf("creating policy: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, policy) })

	eventually(t, "the configuration to be written once", func() error {
		cfg := &unstructured.Unstructured{}
		cfg.SetGroupVersionKind(frrConfigGVK)
		return k8sClient.Get(testCtx, types.NamespacedName{Namespace: ns, Name: defaultFRRConfigName}, cfg)
	})
	time.Sleep(2 * time.Second)

	baseline := announceWrites()
	consistently(t, "the write counter to stay flat while nothing changes", 5*time.Second, func() error {
		if now := announceWrites(); now != baseline {
			return fmt.Errorf("writes went from %v to %v with no input change; "+
				"every one of those is an FRR reload", baseline, now)
		}
		return nil
	})
}

func announceWrites() float64 {
	var sum float64
	for _, op := range []string{"created", "updated"} {
		sum += testutil.ToFloat64(
			metrics.PatchesTotal.WithLabelValues("FRRConfiguration", announceControllerName, op))
	}
	return sum
}

// FRR keeps its previous configuration when a reload fails, so what is already
// advertised survives and anything newly attached silently is not. Nothing else
// in the cluster reports that.
func TestARejectedReloadIsReportedNotSwallowed(t *testing.T) {
	ns := "frr-rejected"
	mustNamespace(t, ns, "")
	mustWorkerNode(t, "rejecting-worker", false)
	mustSubnetWithVPC(t, "external", "ovn-cluster", "10.199.4.0/22")

	state := &unstructured.Unstructured{}
	state.SetGroupVersionKind(frrNodeStateGVK)
	state.SetName("rejecting-worker")
	if err := unstructured.SetNestedMap(state.Object, map[string]any{}, "spec"); err != nil {
		t.Fatalf("building node state: %v", err)
	}
	if err := k8sClient.Create(testCtx, state); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating node state: %v", err)
	}
	eventually(t, "FRR to report a rejection", func() error {
		got := &unstructured.Unstructured{}
		got.SetGroupVersionKind(frrNodeStateGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "rejecting-worker"}, got); err != nil {
			return err
		}
		if err := unstructured.SetNestedField(got.Object,
			"line 7: % Unknown command: no bgp typo-here", "status", "lastReloadResult"); err != nil {
			return err
		}
		return k8sClient.Status().Update(testCtx, got)
	})

	policy := &platformv1alpha1.AnnouncementPolicy{
		ObjectMeta: metav1.ObjectMeta{Name: "default"},
		Spec: platformv1alpha1.AnnouncementPolicySpec{
			BorderPeer:      "10.198.175.254",
			LocalASN:        65030,
			PeerASN:         65000,
			TargetNamespace: ns,
			Nodes:           []string{"rejecting-worker"},
		},
	}
	if err := k8sClient.Create(testCtx, policy); err != nil {
		t.Fatalf("creating policy: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(testCtx, policy) })

	eventually(t, "the rejection to be reported with FRR's own words", func() error {
		got := &platformv1alpha1.AnnouncementPolicy{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: "default"}, got); err != nil {
			return err
		}
		if len(got.Status.ReloadFailures) != 1 {
			return fmt.Errorf("reloadFailures = %+v", got.Status.ReloadFailures)
		}
		if !strings.Contains(got.Status.ReloadFailures[0].Message, "Unknown command") {
			return fmt.Errorf("the failure does not carry FRR's message: %q",
				got.Status.ReloadFailures[0].Message)
		}
		cond := apimeta.FindStatusCondition(got.Status.Conditions, platformv1alpha1.ConditionAccepted)
		if cond == nil || cond.Status != metav1.ConditionFalse {
			return fmt.Errorf("the policy still claims to be accepted: %+v", cond)
		}
		return nil
	})
}
