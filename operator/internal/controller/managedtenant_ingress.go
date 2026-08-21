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

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

// A tenant's apiserver is reached from outside on a name, and the name has to
// lead somewhere.
//
// The certificate already answered for it — `ingressHost` puts it in the SANs —
// and nothing published a route, so the name did not resolve at all. Every one
// of the tenant's twelve conditions was True while `kubectl` could not find the
// host, and the kubeconfig the UI hands out rewrites the server to that name
// unconditionally. A file that cannot work, produced by a green tenant.
//
// The route is a TLS-passthrough match on the SNI: the tenant's own certificate
// reaches the client, and the ingress controller is a forwarder rather than a
// terminator. Anything else would need the tenant's key.
var ingressRouteTCPGVK = schema.GroupVersionKind{
	Group: "traefik.io", Version: "v1alpha1", Kind: "IngressRouteTCP",
}

// externalDNSTargetAnnotation is how the address gets published.
//
// external-dns cannot infer one from an IngressRouteTCP — there is no
// load-balancer status on it, the address belongs to the shared ingress
// Service — so without this the object exists, the route works for anyone who
// already knows the address, and the name still does not resolve. That is the
// half-failure this whole file exists to stop, so its absence is reported
// rather than assumed harmless.
const externalDNSTargetAnnotation = "external-dns.alpha.kubernetes.io/target"

// reconcileExternalRoutes publishes the tenant's apiserver under its name.
func (r *ManagedTenantReconciler) reconcileExternalRoutes(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace, vip string,
) (ready bool, reason, message string, err error) {
	host := r.ingressHost(obj.Name)
	if host == "" {
		// No domain configured: there is no external name, the certificate
		// carries none either, and the kubeconfig keeps the in-cluster address.
		// Consistent, so nothing to report.
		return true, "", "", nil
	}

	target := envOr("TENANTS_EXTERNAL_DNS_TARGET", "")
	if err := r.ensureRoute(ctx, obj, namespace, routeSpec{
		name:       obj.Name + "-api",
		host:       host,
		port:       tenantAPIPort,
		entryPoint: envOr("TENANTS_TRAEFIK_ENTRYPOINT", "websecure"),
		target:     target,
	}); err != nil {
		return false, "", "", err
	}

	// Trustd only where it is reached this way. A tenant with its own VIP
	// publishes 50001 on it and the workers use that — measured: the transit
	// allow for 50001 is what let op-t1's workers join. The product writes this
	// route in both cases, which is where that tenant's unused one came from.
	if obj.Spec.Workers.OS == "talos" && vip == "" {
		if err := r.ensureRoute(ctx, obj, namespace, routeSpec{
			name:       obj.Name + "-trustd",
			host:       fmt.Sprintf("%s.%s.svc", obj.Name, namespace),
			port:       tenantTrustdPort,
			entryPoint: envOr("TENANTS_TRAEFIK_TRUSTD_ENTRYPOINT", "trustd"),
		}); err != nil {
			return false, "", "", err
		}
	}

	if target == "" {
		return false, "NoDNSTarget", fmt.Sprintf(
			"%s is routed, and nothing publishes it: external-dns cannot read an "+
				"address off an IngressRouteTCP, so TENANTS_EXTERNAL_DNS_TARGET "+
				"has to name the ingress address. Until something else creates "+
				"the record, the name does not resolve and the kubeconfig this "+
				"tenant hands out cannot be used.", host), nil
	}
	return true, "Published", fmt.Sprintf("%s via %s", host, target), nil
}

type routeSpec struct {
	name, host, entryPoint, target string
	port                           int
}

func (r *ManagedTenantReconciler) ensureRoute(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace string, want routeSpec,
) error {
	route := &unstructured.Unstructured{}
	route.SetGroupVersionKind(ingressRouteTCPGVK)
	route.SetName(want.name)
	route.SetNamespace(namespace)

	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, route, func() error {
		mergeLabels(route, map[string]string{"kubevirt-ui.io/tenant": obj.Name})
		if want.target != "" {
			mergeAnnotations(route, map[string]string{
				externalDNSTargetAnnotation: want.target,
			})
		}
		spec := map[string]any{
			"entryPoints": []any{want.entryPoint},
			"routes": []any{map[string]any{
				"match": fmt.Sprintf("HostSNI(`%s`)", want.host),
				"services": []any{map[string]any{
					"name": obj.Name,
					// The Kamaji TCP Service's port, which is the tenant's own
					// apiserver port — 6443 here because every tenant this
					// operator builds has its own address.
					"port": int64(want.port),
				}},
			}},
			// Passthrough, always: terminating here would need the tenant's
			// key, and the client is checking a certificate the tenant's own
			// control plane issued.
			"tls": map[string]any{"passthrough": true},
		}
		return unstructured.SetNestedMap(route.Object, spec, "spec")
	})
	if err != nil {
		return fmt.Errorf("publishing %s: %w", want.name, err)
	}
	return nil
}

func routesCondition(ready bool, reason, message string) metav1.Condition {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	if reason == "" {
		reason = "Published"
	}
	return metav1.Condition{
		Type: "ExternallyReachable", Status: status,
		Reason: reason, Message: message,
	}
}
