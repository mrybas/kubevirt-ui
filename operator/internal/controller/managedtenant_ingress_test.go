package controller

import (
	"fmt"
	"testing"

	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
)

func routeOf(t *testing.T, namespace, name string) *unstructured.Unstructured {
	t.Helper()
	route := &unstructured.Unstructured{}
	route.SetGroupVersionKind(ingressRouteTCPGVK)
	if err := k8sReader.Get(testCtx,
		types.NamespacedName{Namespace: namespace, Name: name}, route); err != nil {
		t.Fatalf("reading IngressRouteTCP %s/%s: %v", namespace, name, err)
	}
	return route
}

// TestTheExternalNameIsRouted.
//
// The certificate answered for `<tenant>.<domain>` and nothing routed it, so
// the name did not resolve at all — every one of the tenant's twelve conditions
// True while kubectl could not find the host, and the kubeconfig the UI hands
// out rewriting the server to that name unconditionally. A file that cannot
// work, produced by a green tenant.
func TestTheExternalNameIsRouted(t *testing.T) {
	t.Setenv("TENANTS_INGRESS_DOMAIN", "tenants.example")
	t.Setenv("TENANTS_EXTERNAL_DNS_TARGET", "10.198.175.200")
	mustNamespace(t, "tenant-tir1", "")

	obj := talosTenant("tir1")
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
	}

	ready, reason, message, err := reconciler.reconcileExternalRoutes(
		testCtx, obj, "tenant-tir1", "10.199.0.90")
	if err != nil {
		t.Fatalf("reconcileExternalRoutes: %v", err)
	}
	if !ready {
		t.Fatalf("not published: %s %s", reason, message)
	}

	route := routeOf(t, "tenant-tir1", "tir1-api")
	match, _, _ := unstructured.NestedString(route.Object, "spec", "routes", "0", "match")
	_ = match
	routes, _, _ := unstructured.NestedSlice(route.Object, "spec", "routes")
	first, _ := routes[0].(map[string]any)
	if got := first["match"]; got != "HostSNI(`tir1.tenants.example`)" {
		t.Errorf("match = %v", got)
	}
	service, _ := first["services"].([]any)[0].(map[string]any)
	if service["name"] != "tir1" || service["port"] != int64(6443) {
		t.Errorf("service = %v", service)
	}
	// Passthrough: terminating here would need the tenant's own key.
	if passthrough, _, _ := unstructured.NestedBool(
		route.Object, "spec", "tls", "passthrough"); !passthrough {
		t.Error("TLS is terminated at the ingress")
	}
	// And the address, without which external-dns publishes nothing: the route
	// works for whoever knows the address, and the name still does not resolve.
	if got := route.GetAnnotations()[externalDNSTargetAnnotation]; got != "10.198.175.200" {
		t.Errorf("target annotation = %q", got)
	}
}

// TestATenantWithItsOwnAddressGetsNoTrustdRoute.
//
// It publishes 50001 on that address, and the transit allow for it is what let
// op-t1's workers join. The product writes the route in both cases, which is
// where that tenant's unused one came from.
func TestATenantWithItsOwnAddressGetsNoTrustdRoute(t *testing.T) {
	t.Setenv("TENANTS_INGRESS_DOMAIN", "tenants.example")
	mustNamespace(t, "tenant-tir2", "")

	obj := talosTenant("tir2")
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
	}
	if _, _, _, err := reconciler.reconcileExternalRoutes(
		testCtx, obj, "tenant-tir2", "10.199.0.91"); err != nil {
		t.Fatalf("with a VIP: %v", err)
	}
	trustd := &unstructured.Unstructured{}
	trustd.SetGroupVersionKind(ingressRouteTCPGVK)
	err := k8sReader.Get(testCtx,
		types.NamespacedName{Namespace: "tenant-tir2", Name: "tir2-trustd"}, trustd)
	if err == nil {
		t.Error("a trustd route was written for a tenant that publishes 50001 itself")
	}

	// Without one, the workers reach the signer through the ingress.
	mustNamespace(t, "tenant-tir3", "")
	obj3 := talosTenant("tir3")
	if _, _, _, err := reconciler.reconcileExternalRoutes(
		testCtx, obj3, "tenant-tir3", ""); err != nil {
		t.Fatalf("without a VIP: %v", err)
	}
	route := routeOf(t, "tenant-tir3", "tir3-trustd")
	routes, _, _ := unstructured.NestedSlice(route.Object, "spec", "routes")
	first, _ := routes[0].(map[string]any)
	if got := first["match"]; got != "HostSNI(`tir3.tenant-tir3.svc`)" {
		t.Errorf("match = %v", got)
	}
}

// TestARouteNothingPublishesIsSaidOutLoud.
//
// The object exists and the name still does not resolve, which is the exact
// half-failure this file exists to stop. Reported rather than left to be
// discovered by a kubeconfig that does not work.
func TestARouteNothingPublishesIsSaidOutLoud(t *testing.T) {
	t.Setenv("TENANTS_INGRESS_DOMAIN", "tenants.example")
	t.Setenv("TENANTS_EXTERNAL_DNS_TARGET", "")
	mustNamespace(t, "tenant-tir4", "")

	obj := talosTenant("tir4")
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
	}
	ready, reason, message, err := reconciler.reconcileExternalRoutes(
		testCtx, obj, "tenant-tir4", "10.199.0.92")
	if err != nil {
		t.Fatalf("reconcileExternalRoutes: %v", err)
	}
	if ready || reason != "NoDNSTarget" {
		t.Fatalf("ready=%v reason=%q", ready, reason)
	}
	if !containsAll(message, "does not resolve", "kubeconfig") {
		t.Errorf("the message does not say what it costs: %s", message)
	}
	// The route is still written: whoever has the address can use it, and the
	// record may come from somewhere else.
	routeOf(t, "tenant-tir4", "tir4-api")

	condition := routesCondition(ready, reason, message)
	if condition.Type != "ExternallyReachable" ||
		condition.Status != metav1.ConditionFalse {
		t.Errorf("condition = %+v", condition)
	}
	_ = apimeta.FindStatusCondition
	_ = fmt.Sprint
}

func containsAll(text string, parts ...string) bool {
	for _, part := range parts {
		found := false
		for i := 0; i+len(part) <= len(text); i++ {
			if text[i:i+len(part)] == part {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}
