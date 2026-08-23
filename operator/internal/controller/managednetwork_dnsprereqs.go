package controller

import (
	"context"
	"errors"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/network"
)

var kyvernoPolicyGVK = schema.GroupVersionKind{
	Group: "kyverno.io", Version: "v1", Kind: "ClusterPolicy",
}

// ensureVpcDNSPrereqs builds the ground a VpcDns stands on.
//
// The step that did not survive the handover. kube-ovn's vpc-dns controller
// does nothing until the attachment and the two ConfigMaps exist, and all
// three are this product's to create — the backend's create path makes them
// and the operator's did not. So a VPC created through the operator handed its
// guests a resolver address over DHCP with nothing answering on it, which is
// the whole of E2 and reads exactly like an invented address.
//
// Cluster-wide and idempotent: written per network because that is when they
// are needed, and `kube.Ensure` writes only on a difference, so the second
// network through here writes nothing.
func (r *ManagedNetworkReconciler) ensureVpcDNSPrereqs(
	ctx context.Context, kubeOVNNS, vip, forwardDNS string,
) error {
	if kubeOVNNS == "" || vip == "" {
		return nil
	}

	nad := &unstructured.Unstructured{}
	nad.SetGroupVersionKind(nadGVK)
	nad.SetName(network.VPCDNSNADName)
	nad.SetNamespace("default")
	if _, err := kube.Ensure(ctx, r.Client, networkControllerName, nad, func() error {
		mergeLabels(nad, map[string]string{network.ManagedLabel: "true"})
		return unstructured.SetNestedField(
			nad.Object, network.VPCDNSNADConfig(), "spec", "config")
	}); err != nil {
		return fmt.Errorf("NetworkAttachmentDefinition/%s: %w", network.VPCDNSNADName, err)
	}

	config := &corev1.ConfigMap{}
	config.Name, config.Namespace = network.VPCDNSConfigMap, kubeOVNNS
	if _, err := kube.Ensure(ctx, r.Client, networkControllerName, config, func() error {
		if config.Labels == nil {
			config.Labels = map[string]string{}
		}
		config.Labels[network.ManagedLabel] = "true"
		if config.Data == nil {
			config.Data = map[string]string{}
		}
		for k, v := range network.VPCDNSConfig(vip, forwardDNS) {
			config.Data[k] = v
		}
		return nil
	}); err != nil {
		return fmt.Errorf("ConfigMap/%s: %w", network.VPCDNSConfigMap, err)
	}

	corefile := &corev1.ConfigMap{}
	corefile.Name, corefile.Namespace = network.VPCDNSCorefileConfigMap, kubeOVNNS
	if _, err := kube.Ensure(ctx, r.Client, networkControllerName, corefile, func() error {
		if corefile.Labels == nil {
			corefile.Labels = map[string]string{}
		}
		corefile.Labels[network.ManagedLabel] = "true"
		corefile.Data = map[string]string{"Corefile": network.VPCDNSCorefile(forwardDNS)}
		return nil
	}); err != nil {
		return fmt.Errorf("ConfigMap/%s: %w", network.VPCDNSCorefileConfigMap, err)
	}

	return nil
}

// ensureVpcDNSPolicy tells the pods of this VPC which resolver to use.
//
// The piece a guest actually feels. With bridge binding the guest is served
// DHCP by its own launcher pod and is handed that pod's resolver, so the
// subnet's DHCP options never reach it — the pod is what has to be told, and
// Kyverno tells it at admission. Without this a launcher in a VPC inherits the
// cluster CoreDNS ClusterIP, which has no route from there.
//
// Absent Kyverno is not a failure: the policy cannot be written and the
// network says so through its DNS condition rather than the reconcile ending.
func (r *ManagedNetworkReconciler) ensureVpcDNSPolicy(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork, vip string,
) error {
	if vip == "" {
		return nil
	}
	policy := &unstructured.Unstructured{}
	policy.SetGroupVersionKind(kyvernoPolicyGVK)
	policy.SetName(network.VPCDNSPolicyName(net.Name))

	_, err := kube.Ensure(ctx, r.Client, networkControllerName, policy, func() error {
		mergeLabels(policy, map[string]string{
			network.ManagedLabel: "true",
			"kubevirt-ui.io/vpc": net.Name,
		})
		return unstructured.SetNestedMap(policy.Object, kyvernoDNSSpec(net.Name, vip), "spec")
	})
	if err != nil {
		if apierrors.IsNotFound(err) || meta.IsNoMatchError(err) ||
			runtime.IsNotRegisteredError(err) {
			// No Kyverno here. Said by name, so the network can report what is
			// missing instead of a write that failed.
			return errNoKyverno
		}
		return fmt.Errorf("ClusterPolicy/%s: %w", network.VPCDNSPolicyName(net.Name), err)
	}
	return nil
}

// errNoKyverno is "this cluster cannot be told", not "the write failed".
var errNoKyverno = errors.New("kyverno is not installed")

// kyvernoDNSSpec mutates pods that land on a subnet of this VPC.
//
// Matched by namespace label rather than by name, so system namespaces are
// excluded by construction, and gated on the namespace's logical switch being
// one of this VPC's subnets — read live, so the policy follows subnet changes
// without being re-rendered.
//
// `failurePolicy: Ignore` on purpose: if the context lookup fails, a pod is
// admitted with cluster DNS rather than not admitted at all. Injecting a
// resolver is a convenience; refusing every workload in a namespace is not.
func kyvernoDNSSpec(vpc, vip string) map[string]any {
	return map[string]any{
		"background":    false,
		"failurePolicy": "Ignore",
		"rules": []any{map[string]any{
			"name": "set-dnsconfig",
			"match": map[string]any{"any": []any{map[string]any{
				"resources": map[string]any{
					"kinds": []any{"Pod"},
					"namespaceSelector": map[string]any{
						"matchLabels": map[string]any{network.ManagedLabel: "true"},
					},
				},
			}}},
			"context": []any{
				map[string]any{
					"name": "nsobj",
					"apiCall": map[string]any{
						"urlPath": "/api/v1/namespaces/{{ request.namespace }}",
					},
				},
				map[string]any{
					"name": "vpcSubnets",
					"apiCall": map[string]any{
						"urlPath": "/apis/kubeovn.io/v1/subnets",
						"jmesPath": fmt.Sprintf(
							"items[?spec.vpc=='%s'].metadata.name | @", vpc),
					},
				},
			},
			"preconditions": map[string]any{"all": []any{map[string]any{
				"key":      `{{ nsobj.metadata.annotations."ovn.kubernetes.io/logical_switch" || '' }}`,
				"operator": "AnyIn",
				"value":    "{{ vpcSubnets }}",
			}}},
			"mutate": map[string]any{"patchStrategicMerge": map[string]any{
				"spec": map[string]any{
					"dnsPolicy": "None",
					"dnsConfig": map[string]any{
						"nameservers": []any{vip},
						"searches": []any{
							"{{ request.namespace }}.svc.cluster.local",
							"svc.cluster.local",
							"cluster.local",
						},
						"options": []any{map[string]any{"name": "ndots", "value": "5"}},
					},
				},
			}},
		}},
	}
}

// forwardDNS is the resolver a VpcDns pod sends everything to: the cluster's
// own CoreDNS, by ClusterIP.
func (r *ManagedNetworkReconciler) forwardDNS(ctx context.Context) string {
	svc := &corev1.Service{}
	for _, name := range []string{"kube-dns", "coredns"} {
		if err := r.Get(ctx, types.NamespacedName{
			Namespace: "kube-system", Name: name,
		}, svc); err == nil && svc.Spec.ClusterIP != "" {
			return svc.Spec.ClusterIP
		}
	}
	return ""
}
