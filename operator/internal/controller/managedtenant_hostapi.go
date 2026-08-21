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
	"sort"

	"sigs.k8s.io/yaml"

	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

// The tenant's storage driver runs inside the tenant cluster and creates the
// real volumes on the host: a PVC asked for in there becomes a DataVolume in the
// tenant's namespace out here, hot-plugged into the worker VM. So it needs the
// host apiserver, and the address it was given is the host's own management
// address — reached, measured on the stand, through the tenant's single default
// route to the border.
//
// That is the wrong plane. The two internal networks exist so that storage and
// the control plane survive the gateways: the control plane already does — pull
// the external leg and 6443, 8132, 50001 and NTP keep answering while the
// internet dies — but storage control did not. A border outage leaves the VMs
// running and stops every attach, detach and expand, and any pod waiting on a
// new volume waits for the network to come back.
//
// So the host apiserver is published on the tenant's own VIP, beside its control
// plane, and the credential the driver reads is pointed at it.
const (
	// 6443 on this VIP is the tenant's own apiserver. Port-disjointness on a
	// shared VIP is a test, not an assumption — the NTP service on 123 is here
	// for the same reason.
	hostAPIPort = 6444

	// hostAPIServerName is what the client verifies the certificate against.
	//
	// The connection goes to the VIP, and the VIP is in no SAN of the host
	// apiserver's certificate — measured: it carries the node addresses, the
	// service IP and the management VIP, and each control-plane node serves its
	// own. Adding a per-tenant address to a host certificate is not a thing
	// that can scale, and skipping verification is not a thing worth doing, so
	// the client is told which name to check instead. That name is in every
	// one of those certificates.
	hostAPIServerName = "kubernetes.default.svc"
)

func hostAPIServiceName(tenant string) string { return tenant + "-host-api" }

// ensureHostAPI publishes the host apiserver on the tenant's VIP, or takes the
// publication away again when the tenant has no storage.
//
// Wanted is decided by the credential itself rather than by a flag: the secret
// is the thing the driver reads, so its presence is the fact and not a proxy for
// it. Withdrawing matters as much as publishing — a port left open on a plane
// after the reason for it is gone is the same hole as an allow left behind by a
// departed tenant, and it is only ever noticed by whoever finds it.
func (r *ManagedTenantReconciler) ensureHostAPI(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace, vip string,
) (published bool, message string, err error) {
	wanted := vip != ""
	if wanted {
		source := &corev1.Secret{}
		switch err := r.Get(ctx, types.NamespacedName{
			Namespace: namespace, Name: csiCredentialSecret,
		}, source); {
		case apierrors.IsNotFound(err):
			wanted = false
		case err != nil:
			return false, "", fmt.Errorf("reading %s/%s: %w",
				namespace, csiCredentialSecret, err)
		}
	}

	name := hostAPIServiceName(obj.Name)
	if !wanted {
		for _, object := range []client.Object{
			&corev1.Service{ObjectMeta: metav1.ObjectMeta{
				Namespace: namespace, Name: name}},
			&discoveryv1.EndpointSlice{ObjectMeta: metav1.ObjectMeta{
				Namespace: namespace, Name: name}},
		} {
			if err := kube.Delete(ctx, r.Client, tenantControllerName, object); err != nil {
				return false, "", err
			}
		}
		return false, "", nil
	}

	addresses, err := r.hostAPIAddresses(ctx)
	if err != nil {
		return false, "", err
	}
	if len(addresses) == 0 {
		// Publishing an address that answers nothing is worse than not
		// publishing it: it looks exactly like a server that is down, and the
		// driver's error names neither.
		return false, "the host apiserver has no endpoints to publish", nil
	}

	service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: name}}
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, service, func() error {
		if service.Labels == nil {
			service.Labels = map[string]string{}
		}
		service.Labels["kubevirt-ui.io/managed"] = "true"
		service.Labels["kubevirt-ui.io/tenant"] = obj.Name
		if service.Annotations == nil {
			service.Annotations = map[string]string{}
		}
		service.Annotations["metallb.universe.tf/loadBalancerIPs"] = vip
		// On both services or neither: annotating one of a pair sharing an
		// address leaves the other pending for ever.
		service.Annotations["metallb.universe.tf/allow-shared-ip"] = cpSharingKey(obj.Name)
		service.Spec.Type = corev1.ServiceTypeLoadBalancer
		// No selector, and that is the whole mechanism: the backends are the
		// host's own apiserver endpoints, which are not pods this Service could
		// ever select. They are mirrored below.
		service.Spec.Selector = nil
		// Cluster, not Local: a request arriving on a node with no local
		// endpoint must still be forwarded, and here there are no local
		// endpoints anywhere.
		service.Spec.ExternalTrafficPolicy = corev1.ServiceExternalTrafficPolicyCluster
		service.Spec.Ports = []corev1.ServicePort{{
			Name: "host-api", Port: hostAPIPort,
			TargetPort: intstr.FromInt32(hostAPITargetPort),
			Protocol:   corev1.ProtocolTCP,
		}}
		return nil
	}); err != nil {
		return false, "", fmt.Errorf("publishing the host API: %w", err)
	}

	slice := &discoveryv1.EndpointSlice{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: name}}
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, slice, func() error {
		if slice.Labels == nil {
			slice.Labels = map[string]string{}
		}
		slice.Labels["kubevirt-ui.io/managed"] = "true"
		slice.Labels["kubevirt-ui.io/tenant"] = obj.Name
		// The Service finds its endpoints by this label and nothing else.
		slice.Labels[discoveryv1.LabelServiceName] = name
		slice.Labels[discoveryv1.LabelManagedBy] = tenantControllerName
		slice.AddressType = discoveryv1.AddressTypeIPv4
		slice.Ports = []discoveryv1.EndpointPort{{
			Name: ptr.To("host-api"), Port: ptr.To[int32](hostAPITargetPort),
			Protocol: ptr.To(corev1.ProtocolTCP),
		}}
		// Rebuilt from the live set every pass rather than added to: a control
		// plane node that has gone must stop being a backend, and an endpoint
		// list that only grows sends a share of every request to an address
		// that is not there.
		slice.Endpoints = nil
		for _, address := range addresses {
			slice.Endpoints = append(slice.Endpoints, discoveryv1.Endpoint{
				Addresses:  []string{address},
				Conditions: discoveryv1.EndpointConditions{Ready: ptr.To(true)},
			})
		}
		return nil
	}); err != nil {
		return false, "", fmt.Errorf("mirroring the host API endpoints: %w", err)
	}

	return true, fmt.Sprintf("%s:%d, %d apiserver endpoint(s)",
		vip, hostAPIPort, len(addresses)), nil
}

// hostAPITargetPort is where the apiserver actually listens.
const hostAPITargetPort = 6443

// hostAPIAddresses is where the host apiserver can be found, read from the
// cluster's own record of it rather than from configuration.
//
// `default/kubernetes` is maintained by the apiservers themselves, so it follows
// a control plane being rebuilt, scaled or replaced with no help from here and
// nothing to keep in step.
func (r *ManagedTenantReconciler) hostAPIAddresses(ctx context.Context) ([]string, error) {
	slices := &discoveryv1.EndpointSliceList{}
	if err := r.List(ctx, slices, client.InNamespace("default"),
		client.MatchingLabels{discoveryv1.LabelServiceName: "kubernetes"}); err != nil {
		return nil, fmt.Errorf("reading the host apiserver endpoints: %w", err)
	}

	seen := map[string]bool{}
	var out []string
	for i := range slices.Items {
		if slices.Items[i].AddressType != discoveryv1.AddressTypeIPv4 {
			continue
		}
		for _, endpoint := range slices.Items[i].Endpoints {
			if endpoint.Conditions.Ready != nil && !*endpoint.Conditions.Ready {
				continue
			}
			for _, address := range endpoint.Addresses {
				if !seen[address] {
					seen[address] = true
					out = append(out, address)
				}
			}
		}
	}
	// Sorted so an unchanged set is an unchanged object: the API returns these
	// in no particular order, and writing them in that order would make every
	// pass a patch and every patch a rewrite of somebody's endpoint list.
	sort.Strings(out)
	return out, nil
}

// throughTheTransitPlane rewrites the driver's kubeconfig to reach the host
// apiserver on the tenant's own VIP.
//
// The host-side secret is left exactly as it is: it is the product's, other
// things read it, and it is correct for a reader on the host network. Only the
// copy that lands inside the tenant — the one the driver actually opens — is
// pointed at the transit address.
//
// `tls-server-name` is what makes this work without touching any certificate.
// The VIP is in no SAN and cannot be: the host certificates are per node and a
// per-tenant address cannot be added to them. Every one of those certificates
// does carry `kubernetes.default.svc`, so the client is told to verify that
// name while connecting to the address. Skipping verification would have been
// one line and would have handed anyone on the transit plane a way to be the
// host apiserver.
func throughTheTransitPlane(kubeconfig []byte, vip string) ([]byte, error) {
	if vip == "" {
		return kubeconfig, nil
	}
	var parsed map[string]any
	if err := yaml.Unmarshal(kubeconfig, &parsed); err != nil {
		// Unreadable is left alone rather than replaced. A kubeconfig this code
		// does not understand is still the one the driver has been using, and
		// a rewrite that guesses at its shape would break a working tenant.
		return kubeconfig, fmt.Errorf("reading the driver kubeconfig: %w", err)
	}
	clusters, ok := parsed["clusters"].([]any)
	if !ok || len(clusters) == 0 {
		return kubeconfig, fmt.Errorf("the driver kubeconfig names no cluster")
	}
	changed := false
	for _, entry := range clusters {
		item, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		cluster, ok := item["cluster"].(map[string]any)
		if !ok {
			continue
		}
		server := fmt.Sprintf("https://%s:%d", vip, hostAPIPort)
		if cluster["server"] != server || cluster["tls-server-name"] != hostAPIServerName {
			cluster["server"] = server
			cluster["tls-server-name"] = hostAPIServerName
			changed = true
		}
	}
	if !changed {
		return kubeconfig, nil
	}
	out, err := yaml.Marshal(parsed)
	if err != nil {
		return kubeconfig, fmt.Errorf("writing the driver kubeconfig: %w", err)
	}
	return out, nil
}

// hostAPIPublished says whether this tenant's VIP carries the host API.
//
// Read from the Service rather than recomputed from the credential: the ACL and
// the publication must agree, and the way to make two things agree is for one to
// read the other rather than for both to derive the same answer separately.
func (r *ManagedTenantReconciler) hostAPIPublished(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) (bool, error) {
	service := &corev1.Service{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: hostAPIServiceName(obj.Name),
	}, service)
	switch {
	case apierrors.IsNotFound(err):
		return false, nil
	case err != nil:
		return false, fmt.Errorf("reading the host API service: %w", err)
	}
	return true, nil
}
