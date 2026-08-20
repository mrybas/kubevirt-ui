package controller

import (
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// cpService returns the error rather than failing: it is called from inside
// eventually, and a Fatalf there turns "not yet" into "never".
func cpService(tenantName string) (*corev1.Service, error) {
	service := &corev1.Service{}
	err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-" + tenantName, Name: tenantName + "-cp-lb",
	}, service)
	return service, err
}

// uncachedCPService is the same read straight from the API server, for the
// assertions that turn on a Service not being there.
func uncachedCPService(tenantName string) (*corev1.Service, error) {
	service := &corev1.Service{}
	err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-" + tenantName, Name: tenantName + "-cp-lb",
	}, service)
	return service, err
}

func mustCPService(t *testing.T, tenantName string) *corev1.Service {
	t.Helper()
	service, err := cpService(tenantName)
	if err != nil {
		t.Fatalf("reading %s's cp-lb: %v", tenantName, err)
	}
	return service
}

// assignAddress is MetalLB's part, which envtest has nobody to play.
func assignAddress(t *testing.T, tenantName, ip string) {
	t.Helper()
	// Patched rather than read-then-updated: this client reads from a cache,
	// and a read-modify-write against an object the controller is also touching
	// loses the race often enough to make a test flake.
	service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{
		Namespace: "tenant-" + tenantName, Name: tenantName + "-cp-lb",
	}}
	patch := []byte(fmt.Sprintf(
		`{"status":{"loadBalancer":{"ingress":[{"ip":%q}]}}}`, ip))
	if err := k8sClient.Status().Patch(testCtx, service,
		client.RawPatch(types.MergePatchType, patch)); err != nil {
		t.Fatalf("assigning %s to %s: %v", ip, tenantName, err)
	}
}

// TestTheTenantAsksForAnAddressAndPublishesTheOneItGets.
//
// The product asked and then waited for the answer inside the request that
// created the tenant, up to a minute. Here the wait is the ordinary state of a
// controller: pending is a condition with a reason, and the address appears in
// status when it appears at all.
func TestTheTenantAsksForAnAddressAndPublishesTheOneItGets(t *testing.T) {
	mustTenant(t, vpcTenant("tad"))

	eventually(t, "the request for an address", func() error {
		service, err := cpService("tad")
		if err != nil {
			return err
		}
		if service.Spec.Type != corev1.ServiceTypeLoadBalancer {
			return fmt.Errorf("type = %s", service.Spec.Type)
		}
		if got := service.Annotations["metallb.universe.tf/address-pool"]; got == "" {
			return fmt.Errorf("no pool asked for")
		}
		if got := service.Annotations["metallb.universe.tf/allow-shared-ip"]; got != "tad-cp" {
			return fmt.Errorf("sharing key = %q", got)
		}
		// Out of Cilium's LB DNAT, or in-VPC pod traffic is rewritten before
		// kube-ovn routes it.
		if got := service.Annotations["service.cilium.io/type"]; got != "ClusterIP" {
			return fmt.Errorf("cilium annotation = %q", got)
		}
		if service.Spec.LoadBalancerIP != "" {
			return fmt.Errorf("it named an address (%s) instead of asking the "+
				"pool for one", service.Spec.LoadBalancerIP)
		}
		return nil
	})

	// A cloud-init tenant publishes two ports; trustd belongs to Talos alone.
	ports := map[string]int32{}
	for _, port := range mustCPService(t, "tad").Spec.Ports {
		ports[port.Name] = port.Port
	}
	if ports["api"] != 6443 || ports["konn"] != 8132 {
		t.Errorf("ports = %v", ports)
	}
	if _, found := ports["trustd"]; found {
		t.Error("a cloud-init tenant published trustd, which nothing there dials")
	}

	// Pending says why, and says nothing about an address it does not have.
	pending := tenantCondition(getTenant(t, "tad"),
		platformv1alpha1.ConditionAddressAssigned)
	if pending == nil || pending.Status != metav1.ConditionFalse {
		t.Fatalf("condition = %+v", pending)
	}
	if getTenant(t, "tad").Status.ControlPlaneVIP != "" {
		t.Error("an address was published before one was assigned")
	}

	assignAddress(t, "tad", "10.199.0.101")
	eventually(t, "the address to be published", func() error {
		obj := getTenant(t, "tad")
		if obj.Status.ControlPlaneVIP != "10.199.0.101" {
			return fmt.Errorf("vip = %q", obj.Status.ControlPlaneVIP)
		}
		condition := tenantCondition(obj, platformv1alpha1.ConditionAddressAssigned)
		if condition == nil || condition.Status != metav1.ConditionTrue {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})

	// And it is not taken away again. Reconciles keep happening; an address
	// that disappears from status because one pass read a stale Service would
	// be worse than never publishing it.
	consistently(t, "the address to stay", 5*time.Second, func() error {
		if got := getTenant(t, "tad").Status.ControlPlaneVIP; got != "10.199.0.101" {
			return fmt.Errorf("vip = %q", got)
		}
		return nil
	})
}

// TestATalosTenantPublishesTrustd. Talos derives trustd's address from the
// control-plane endpoint and dials :50001 there. The port is not configurable,
// which is the whole reason each tenant needs an address of its own.
func TestATalosTenantPublishesTrustd(t *testing.T) {
	mustTenant(t, vpcTalosTenant("tadt"))

	eventually(t, "trustd on the tenant's own address", func() error {
		service, err := cpService("tadt")
		if err != nil {
			return err
		}
		for _, port := range service.Spec.Ports {
			if port.Name == "trustd" && port.Port == 50001 {
				return nil
			}
		}
		return fmt.Errorf("no trustd port")
	})
}

// TestAPoolTheSubnetDoesNotExcludeIsRefusedBeforeAnAddressIsHandedOut.
//
// kube-ovn allocates router legs and EIPs from the transit subnet. A pool range
// it does not exclude is an address both allocators can give out, and the loser
// finds out as an outage. So the refusal has to come before the Service exists:
// the damage is done by an address being handed out, not by asking for one.
func TestAPoolTheSubnetDoesNotExcludeIsRefusedBeforeAnAddressIsHandedOut(t *testing.T) {
	// The namespace exists from the start, so "no Service" means the refusal
	// stopped it rather than the write having nowhere to land.
	mustNamespace(t, "tenant-tadx", "")
	mustPool(t, "overlapping", "10.199.9.100-10.199.9.120")
	mustExcludingSubnet(t, "transit-bad", "10.199.9.0/24", []string{"10.199.9.1..10.199.9.50"})

	reconciler := &ManagedTenantReconciler{
		Client:           k8sClient,
		Scheme:           k8sClient.Scheme(),
		APIReader:        k8sClient,
		MetalLBPool:      "overlapping",
		MetalLBNamespace: "kubevirt-ui-system",
		TransitSubnet:    "transit-bad",
	}
	obj := vpcTenant("tadx")
	vip, _, ready, message, err := reconciler.reconcileAddress(testCtx, obj, "tenant-tadx")
	if err != nil {
		t.Fatalf("reconcileAddress: %v", err)
	}
	if ready || vip != "" {
		t.Fatalf("it went ahead: vip=%q ready=%v", vip, ready)
	}
	if !strings.Contains(message, "10.199.9.100-10.199.9.120") {
		t.Errorf("the refusal does not name the range: %s", message)
	}
	if got := addressCondition(false, message).Reason; got != "PoolOverlapsSubnet" {
		t.Errorf("reason = %q", got)
	}

	// Straight to the API server: a cached read cannot tell "was not created"
	// from "has not arrived", and here the first of those is the whole claim.
	service := &corev1.Service{}
	err = k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tadx", Name: "tadx-cp-lb",
	}, service)
	if err == nil {
		t.Error("it asked for an address anyway")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the Service: %v", err)
	}

	// The same tenant against a subnet that does exclude the pool goes ahead,
	// so the refusal above is the check working rather than the call failing.
	mustExcludingSubnet(t, "transit-good", "10.199.9.0/24", []string{"10.199.9.1..10.199.9.255"})
	reconciler.TransitSubnet = "transit-good"
	if _, _, _, _, err := reconciler.reconcileAddress(testCtx, obj, "tenant-tadx"); err != nil {
		t.Fatalf("reconcileAddress against a well-excluded pool: %v", err)
	}
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tadx", Name: "tadx-cp-lb",
	}, service); err != nil {
		t.Fatalf("the Service was not asked for even with the pool excluded: %v", err)
	}
}

// TestAnUnconfiguredTransitSubnetSkipsTheCheckInsteadOfBlocking. A diagnostic
// that stops tenants from being created because it could not read something has
// stopped being a diagnostic.
func TestAnUnconfiguredTransitSubnetSkipsTheCheckInsteadOfBlocking(t *testing.T) {
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sClient,
	}
	for _, subnet := range []string{"", "there-is-no-such-subnet"} {
		reconciler.TransitSubnet = subnet
		refusal, err := reconciler.poolOverlapsSubnet(testCtx, "no-such-pool")
		if err != nil {
			t.Fatalf("subnet %q: %v", subnet, err)
		}
		if refusal != "" {
			t.Errorf("subnet %q refused: %s", subnet, refusal)
		}
	}
}

func mustPool(t *testing.T, name, addresses string) {
	t.Helper()
	pool := &unstructured.Unstructured{}
	pool.SetGroupVersionKind(ipAddressPoolGVK)
	pool.SetName(name)
	pool.SetNamespace("kubevirt-ui-system")
	_ = unstructured.SetNestedStringSlice(pool.Object,
		[]string{addresses}, "spec", "addresses")
	if err := k8sClient.Create(testCtx, pool); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating pool %s: %v", name, err)
	}
}

func mustExcludingSubnet(t *testing.T, name, cidr string, exclude []string) {
	t.Helper()
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(ovnSubnetGVK)
	subnet.SetName(name)
	_ = unstructured.SetNestedField(subnet.Object, cidr, "spec", "cidrBlock")
	_ = unstructured.SetNestedStringSlice(subnet.Object, exclude, "spec", "excludeIps")
	if err := k8sClient.Create(testCtx, subnet); err != nil {
		if !apierrors.IsAlreadyExists(err) {
			t.Fatalf("creating subnet %s: %v", name, err)
		}
		live := &unstructured.Unstructured{}
		live.SetGroupVersionKind(ovnSubnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{Name: name}, live); err != nil {
			t.Fatalf("reading subnet %s: %v", name, err)
		}
		_ = unstructured.SetNestedStringSlice(live.Object, exclude, "spec", "excludeIps")
		if err := k8sClient.Update(testCtx, live); err != nil {
			t.Fatalf("updating subnet %s: %v", name, err)
		}
	}
}

// TestATenantOnTheDefaultOverlayAsksForNoAddress.
//
// Its control plane is reached by the Kamaji Service's ClusterIP, which is
// natively routable there. The pool is twenty addresses on this lab; handing
// one to every tenant that will never dial it is how it runs out.
func TestATenantOnTheDefaultOverlayAsksForNoAddress(t *testing.T) {
	mustTenant(t, plainTenant("tadd"))

	eventually(t, "the tenant to settle", func() error {
		if tenantCondition(getTenant(t, "tadd"),
			platformv1alpha1.ConditionNamespaceReady) == nil {
			return fmt.Errorf("not reconciled yet")
		}
		return nil
	})

	if _, err := uncachedCPService("tadd"); err == nil {
		t.Error("an address was asked for on the default overlay")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the Service: %v", err)
	}
	obj := getTenant(t, "tadd")
	for _, kind := range []string{
		platformv1alpha1.ConditionAddressAssigned,
		platformv1alpha1.ConditionTimeServed,
	} {
		if got := tenantCondition(obj, kind); got != nil {
			t.Errorf("it reports %s = %+v about something it does not have",
				kind, got)
		}
	}
	// And no time Service either: on the default overlay a worker reaches the
	// public servers the same way it reaches everything else.
	service := &corev1.Service{}
	err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "kubevirt-ui-system", Name: "tadd-ntp",
	}, service)
	if err == nil {
		t.Error("a time Service was published for a tenant with no address")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the time Service: %v", err)
	}
}

// TestATalosTenantOnTheDefaultOverlayGetsANamedCertificate.
//
// No address means no IP SAN — and cert-manager refuses a certificate with an
// empty one, so this is not a cosmetic difference. The worker there dials the
// Service by name and the name is what has to be answered for.
func TestATalosTenantOnTheDefaultOverlayGetsANamedCertificate(t *testing.T) {
	mustTenant(t, talosTenant("tadn"))

	eventually(t, "the signer certificate", func() error {
		_, err := certManagerObject("Certificate", "tenant-tadn", "tadn-talos-signer")
		return err
	})
	signer, err := certManagerObject("Certificate", "tenant-tadn", "tadn-talos-signer")
	if err != nil {
		t.Fatalf("reading the signer certificate: %v", err)
	}
	if addresses, found, _ := unstructured.NestedSlice(
		signer.Object, "spec", "ipAddresses"); found && len(addresses) > 0 {
		t.Errorf("ipAddresses = %v on a tenant that has no address", addresses)
	}
	names, _, _ := unstructured.NestedStringSlice(signer.Object, "spec", "dnsNames")
	if len(names) != 2 {
		t.Errorf("dnsNames = %v", names)
	}
}
