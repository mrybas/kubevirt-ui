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
	"context"
	"fmt"
	"os"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/tenant"
)

const (
	poolEnv          = "TENANTS_CP_METALLB_POOL"
	poolNamespaceEnv = "TENANTS_CP_METALLB_NAMESPACE"
	transitSubnetEnv = "TENANTS_CP_TRANSIT_SUBNET"

	defaultPool          = "traefik"
	defaultPoolNamespace = "o0-metallb"

	tenantAPIPort    = 6443
	tenantKonnPort   = 8132
	tenantTrustdPort = 50001
)

var ipAddressPoolGVK = schema.GroupVersionKind{
	Group: "metallb.io", Version: "v1beta1", Kind: "IPAddressPool",
}

var ovnSubnetGVK = schema.GroupVersionKind{
	Group: "kubeovn.io", Version: "v1", Kind: "Subnet",
}

func (r *ManagedTenantReconciler) pool() string {
	if r.MetalLBPool != "" {
		return r.MetalLBPool
	}
	if name := os.Getenv(poolEnv); name != "" {
		return name
	}
	return defaultPool
}

func (r *ManagedTenantReconciler) poolNamespace() string {
	if r.MetalLBNamespace != "" {
		return r.MetalLBNamespace
	}
	if name := os.Getenv(poolNamespaceEnv); name != "" {
		return name
	}
	return defaultPoolNamespace
}

// transitSubnet is the kube-ovn subnet whose excludeIps must contain the pool.
// Empty means the check does not run — there is no safe guess for it, and
// checking the wrong subnet would report a danger that is not there.
func (r *ManagedTenantReconciler) transitSubnet() string {
	if r.TransitSubnet != "" {
		return r.TransitSubnet
	}
	return os.Getenv(transitSubnetEnv)
}

// cpSharingKey is the MetalLB key every Service on this tenant's address must
// carry.
//
// Every one of them: MetalLB refuses the second Service outright when only it
// declares the key, and the address stays pending forever — which presents as a
// server that does not answer rather than as a configuration error. The key
// contains the tenant name, so sharing between tenants stays impossible.
func cpSharingKey(name string) string {
	return name + "-cp"
}

// cpPorts is api and konnectivity for every tenant, trustd only where Talos
// needs it — Talos dials 50001 and nothing else, which is the whole reason each
// tenant needs an address of its own.
func cpPorts(obj *platformv1alpha1.ManagedTenant) []corev1.ServicePort {
	ports := []corev1.ServicePort{
		{Name: "api", Port: tenantAPIPort,
			TargetPort: intstr.FromInt32(tenantAPIPort), Protocol: corev1.ProtocolTCP},
		{Name: "konn", Port: tenantKonnPort,
			TargetPort: intstr.FromInt32(tenantKonnPort), Protocol: corev1.ProtocolTCP},
	}
	if obj.Spec.Workers.OS == "talos" {
		ports = append(ports, corev1.ServicePort{
			Name: "trustd", Port: tenantTrustdPort,
			TargetPort: intstr.FromInt32(tenantTrustdPort), Protocol: corev1.ProtocolTCP,
		})
	}
	return ports
}

// reconcileAddress gives the tenant its own address and reports what came back.
//
// The product asked for the address and then waited for it, up to a minute,
// inside the request that created the tenant. Here the waiting is the normal
// state of a controller and the answer is a condition, so a pool that is full
// says so instead of timing out somebody's API call.
func (r *ManagedTenantReconciler) reconcileAddress(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) (vip string, ready bool, message string, err error) {
	pool := r.pool()

	if refusal, err := r.poolOverlapsSubnet(ctx, pool); err != nil {
		return "", false, "", err
	} else if refusal != "" {
		// Refused before the Service exists, because the damage is done by the
		// address being handed out, not by asking for one.
		return "", false, refusal, nil
	}

	service := &corev1.Service{}
	service.Name = obj.Name + "-cp-lb"
	service.Namespace = namespace
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, service, func() error {
		if service.Labels == nil {
			service.Labels = map[string]string{}
		}
		service.Labels["kubevirt-ui.io/managed"] = "true"
		service.Labels["kubevirt-ui.io/tenant"] = obj.Name
		if service.Annotations == nil {
			service.Annotations = map[string]string{}
		}
		// Out of Cilium's LB BPF DNAT, so in-VPC pod traffic is not rewritten
		// before kube-ovn gets to route it.
		service.Annotations["service.cilium.io/type"] = "ClusterIP"
		service.Annotations["metallb.universe.tf/address-pool"] = pool
		service.Annotations["metallb.universe.tf/allow-shared-ip"] = cpSharingKey(obj.Name)
		service.Spec.Type = corev1.ServiceTypeLoadBalancer
		service.Spec.Selector = map[string]string{"kamaji.clastix.io/name": obj.Name}
		service.Spec.Ports = cpPorts(obj)
		// No loadBalancerIP: MetalLB picks from the pool, and naming an address
		// here is how two tenants end up asking for the same one.
		return nil
	}); err != nil {
		return "", false, "", fmt.Errorf("asking for %s's address: %w", obj.Name, err)
	}

	for _, ingress := range service.Status.LoadBalancer.Ingress {
		if ingress.IP != "" {
			return ingress.IP, true, fmt.Sprintf("%s, from pool %q", ingress.IP, pool), nil
		}
	}
	return "", false, fmt.Sprintf(
		"%s/%s has no address yet. The usual cause is an exhausted pool %q: "+
			"every address in it is held by another tenant. Append a range to "+
			"the IPAddressPool — append, never resize a range in use.",
		namespace, service.Name, pool), nil
}

// poolOverlapsSubnet is the invariant behind every tenant address: the MetalLB
// pool must sit inside the transit subnet's excludeIps.
//
// kube-ovn allocates router legs and EIPs out of that subnet. A pool range it
// does not exclude is an address both allocators can hand out, and the loser
// finds out as a duplicate-address outage rather than as a conflict.
//
// Silent when either object cannot be read. A diagnostic that stops tenants
// from being created because it could not read something has stopped being a
// diagnostic.
func (r *ManagedTenantReconciler) poolOverlapsSubnet(
	ctx context.Context, pool string,
) (string, error) {
	subnetName := r.transitSubnet()
	if subnetName == "" {
		return "", nil
	}

	// Read straight from the API server: these are two occasional reads of
	// objects in other people's namespaces, and caching them would mean
	// watching every IPAddressPool and Subnet in the cluster for the life of
	// the process.
	reader := r.reader()

	poolObj := &unstructured.Unstructured{}
	poolObj.SetGroupVersionKind(ipAddressPoolGVK)
	if err := reader.Get(ctx, types.NamespacedName{
		Namespace: r.poolNamespace(), Name: pool,
	}, poolObj); err != nil {
		if unreadable(err) {
			return "", nil
		}
		return "", fmt.Errorf("reading IPAddressPool %s: %w", pool, err)
	}

	subnetObj := &unstructured.Unstructured{}
	subnetObj.SetGroupVersionKind(ovnSubnetGVK)
	if err := reader.Get(ctx, types.NamespacedName{Name: subnetName}, subnetObj); err != nil {
		if unreadable(err) {
			return "", nil
		}
		return "", fmt.Errorf("reading Subnet %s: %w", subnetName, err)
	}

	addresses, _, _ := unstructured.NestedStringSlice(poolObj.Object, "spec", "addresses")
	excluded, _, _ := unstructured.NestedStringSlice(subnetObj.Object, "spec", "excludeIps")

	uncovered := tenant.Uncovered(addresses, excluded)
	if len(uncovered) == 0 {
		return "", nil
	}
	shown := make([]string, 0, len(uncovered))
	for _, span := range uncovered {
		shown = append(shown, span.String())
	}
	return fmt.Sprintf(
		"MetalLB pool %q has ranges outside the excludeIps of subnet %q: %s. "+
			"kube-ovn allocates router legs and EIPs from that subnet, so it can "+
			"hand out an address MetalLB has already given to a tenant VIP. Add "+
			"the range to the subnet's excludeIps before creating tenants on "+
			"this pool.", pool, subnetName, strings.Join(shown, ", ")), nil
}

// unreadable covers the three ways this check can simply not apply: the object
// is absent, this deployment may not read it, or the CRD is not installed.
func unreadable(err error) bool {
	return apierrors.IsNotFound(err) || apierrors.IsForbidden(err) ||
		apimeta.IsNoMatchError(err)
}

// reader is the uncached client when one was wired, the cached one otherwise.
func (r *ManagedTenantReconciler) reader() client.Reader {
	if r.APIReader != nil {
		return r.APIReader
	}
	return r.Client
}

func addressCondition(ready bool, message string) metav1.Condition {
	status := metav1.ConditionFalse
	reason := "Pending"
	if ready {
		status = metav1.ConditionTrue
		reason = "Assigned"
	} else if strings.HasPrefix(message, "MetalLB pool") {
		reason = "PoolOverlapsSubnet"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionAddressAssigned,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}
