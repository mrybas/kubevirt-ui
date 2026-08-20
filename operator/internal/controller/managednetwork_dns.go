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
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"sync"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/yaml"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/network"
)

var vpcDNSGVK = schema.GroupVersionKind{Group: "kubeovn.io", Version: "v1", Kind: "VpcDns"}

const (
	// serviceRouteAnnotation is honoured by kube-ovn's CNI on the VpcDns pod
	// template, and the VpcDns controller leaves it alone.
	serviceRouteAnnotation = "ovn-nad.default.ovn.kubernetes.io/routes"

	// overlaySubnet is the default cluster overlay; its gateway is the next hop
	// for the service network.
	overlaySubnet = "ovn-default"
)

// serviceCIDRArg finds the range in the apiserver's own command line.
var serviceCIDRArg = regexp.MustCompile(`--service-cluster-ip-range=([^\s"]+)`)

// serviceCIDRCache memoises the discovered range. A cluster does not change its
// service network while the process runs, and the lookup is an uncached read.
type serviceCIDRCache struct {
	once  sync.Once
	value string
}

// wantsDNS defaults to true: a network whose workloads cannot resolve anything
// is not a useful network, and the old create path always built one.
func wantsDNS(net *platformv1alpha1.ManagedNetwork) bool {
	return net.Spec.VPCDNS == nil || *net.Spec.VPCDNS
}

// reconcileDNS gives the network a resolver and keeps the one route that
// resolver needs.
//
// The route is the reason this belongs in a controller. It used to be applied
// once, best-effort, at create time — the Deployment is made by kube-ovn *after*
// the VpcDns object, so on the create path it usually was not there yet, and
// the only thing that ever applied it later was a person calling the recreate
// endpoint. Here it is checked on every pass and a Deployment write wakes it.
func (r *ManagedNetworkReconciler) reconcileDNS(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork, kubeOVNNS string,
) error {
	if !wantsDNS(net) {
		net.Status.ServiceRoute = ""
		r.setNetworkCondition(net, platformv1alpha1.ConditionDNSReady, true, "NotRequested",
			"vpcDNS=false — workloads in this network resolve nothing unless "+
				"something else provides a resolver")
		return nil
	}

	if err := r.ensureVpcDNS(ctx, net); err != nil {
		r.setNetworkCondition(net, platformv1alpha1.ConditionDNSReady, false, "WriteFailed", err.Error())
		return nil
	}

	route, reason, message := r.ensureServiceRoute(ctx, net, kubeOVNNS)
	net.Status.ServiceRoute = route
	if route == "" {
		r.setNetworkCondition(net, platformv1alpha1.ConditionDNSReady, false, reason, message)
		return nil
	}
	r.setNetworkCondition(net, platformv1alpha1.ConditionDNSReady, true, "Routed", message)
	return nil
}

// resolveDNSServer is the address workloads are handed over DHCP.
//
// Declared wins; otherwise it is read from the ConfigMap that configures
// kube-ovn's VpcDns controller, which is where the cluster already states it.
// Absent from both, nothing is promised — a DHCP option pointing at a resolver
// that is not there behaves exactly like a working one until something resolves.
func (r *ManagedNetworkReconciler) resolveDNSServer(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork, kubeOVNNS string,
) string {
	if net.Spec.DNSServer != "" {
		return net.Spec.DNSServer
	}
	if !wantsDNS(net) || kubeOVNNS == "" {
		return ""
	}
	// `vpc-dns-config` is where kube-ovn's own VpcDns controller is configured,
	// and therefore where the cluster already states this address. Reading it
	// beats carrying the same address in two places.
	cm := &corev1.ConfigMap{}
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: kubeOVNNS, Name: vpcDNSConfigMap,
	}, cm); err != nil {
		return ""
	}
	return strings.TrimSpace(cm.Data["coredns-vip"])
}

// ensureVpcDNS writes the per-network resolver.
func (r *ManagedNetworkReconciler) ensureVpcDNS(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) error {
	name := net.Name + "-dns"
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(vpcDNSGVK)
	live.SetName(name)

	_, err := kube.Ensure(ctx, r.Client, networkControllerName, live, func() error {
		mergeLabels(live, map[string]string{
			network.ManagedLabel: "true",
			"kubevirt-ui.io/vpc": net.Name,
		})
		return unstructured.SetNestedMap(live.Object, map[string]any{
			"vpc":    net.Name,
			"subnet": network.DefaultSubnetName(net),
		}, "spec")
	})
	if err != nil {
		return fmt.Errorf("VpcDns/%s: %w", name, err)
	}
	return nil
}

// ensureServiceRoute puts the service network on the VpcDns pod template.
//
// A VpcDns pod's secondary interface gets exactly one route into the default
// overlay — the apiserver's ClusterIP as a /32, written by kube-ovn and not
// configurable — so the cluster resolver's ClusterIP is *not* reachable: that
// packet takes the pod's default route out into the tenant network and dies
// there. Routing the whole service network is what removes the need to pin
// resolver pod addresses in the Corefile, and pinned pod addresses is precisely
// how VPC DNS once went silent while every object still reported healthy.
func (r *ManagedNetworkReconciler) ensureServiceRoute(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork, kubeOVNNS string,
) (route, reason, message string) {
	serviceCIDR, err := r.serviceCIDR(ctx, net)
	if err != nil {
		return "", "ServiceCIDRUnknown", err.Error()
	}
	gateway, err := r.overlayGateway(ctx)
	if err != nil {
		return "", "OverlayGatewayUnknown", err.Error()
	}

	name := "vpc-dns-" + net.Name + "-dns"
	deployment := &appsv1.Deployment{}
	err = r.Get(ctx, types.NamespacedName{Namespace: kubeOVNNS, Name: name}, deployment)
	if apierrors.IsNotFound(err) {
		// kube-ovn creates it after the VpcDns object, so this is the normal
		// state for a few seconds. It is a waiting state and not a failure —
		// but it is reported, because "waiting" that never ends is the bug this
		// replaced.
		return "", "DeploymentPending",
			fmt.Sprintf("%s/%s does not exist yet; the route goes on as soon as "+
				"kube-ovn creates it", kubeOVNNS, name)
	}
	if err != nil {
		return "", "Unreadable", err.Error()
	}

	want, _ := json.Marshal([]map[string]string{{"dst": serviceCIDR, "gw": gateway}})
	if deployment.Spec.Template.Annotations[serviceRouteAnnotation] == string(want) {
		return serviceCIDR + " via " + gateway, "Routed",
			fmt.Sprintf("%s reaches the cluster resolver via %s", name, gateway)
	}

	patched := deployment.DeepCopy()
	if patched.Spec.Template.Annotations == nil {
		patched.Spec.Template.Annotations = map[string]string{}
	}
	patched.Spec.Template.Annotations[serviceRouteAnnotation] = string(want)
	if err := r.Patch(ctx, patched, client.MergeFrom(deployment)); err != nil {
		return "", "RouteWriteFailed", err.Error()
	}
	kube.CountWrite(r.Scheme, patched, networkControllerName, "updated")
	return serviceCIDR + " via " + gateway, "Routed",
		fmt.Sprintf("%s reaches the cluster resolver via %s", name, gateway)
}

// serviceCIDR is the cluster's service network.
//
// Read through the uncached reader on purpose: it is asked for once per process
// and the alternative is an informer over every Pod and ConfigMap in
// kube-system, which is a large cache for a value that never changes.
func (r *ManagedNetworkReconciler) serviceCIDR(
	ctx context.Context, net *platformv1alpha1.ManagedNetwork,
) (string, error) {
	// Three sources, most specific first. The last one is discovery, and
	// discovery is the one that can be unavailable: on a managed control plane
	// there is no apiserver pod to read and no kubeadm ConfigMap either, so the
	// cluster genuinely cannot be asked. That is a configuration case, not a
	// failure, and both of the other two exist so nobody has to work around it.
	if net.Spec.ServiceCIDR != "" {
		return net.Spec.ServiceCIDR, nil
	}
	// A cluster-wide fact belongs on the operator, not repeated on every
	// network — one flag, set once, for the installs where discovery cannot
	// work.
	if r.ServiceCIDR != "" {
		return r.ServiceCIDR, nil
	}
	if r.APIReader == nil {
		return "", fmt.Errorf(
			"no uncached reader available; set --service-cidr on the operator " +
				"or spec.serviceCIDR on this network")
	}

	r.serviceCIDRs.once.Do(func() {
		r.serviceCIDRs.value = discoverServiceCIDR(ctx, r.APIReader)
	})
	if r.serviceCIDRs.value == "" {
		return "", fmt.Errorf(
			"cannot find the cluster service network: no kube-system/kubeadm-config, " +
				"and no readable kube-apiserver pod exposing " +
				"--service-cluster-ip-range — which is the normal state on a " +
				"managed control plane. Set --service-cidr on the operator, or " +
				"spec.serviceCIDR on this network")
	}
	return r.serviceCIDRs.value, nil
}

// discoverServiceCIDR asks the cluster what its service network is.
//
// Two sources, because distributions disagree. kubeadm writes it into a
// ConfigMap; Talos and others do not have that ConfigMap at all and the only
// statement of the fact is the apiserver's own command line. Measured on this
// stand: no kubeadm-config, and `--service-cluster-ip-range=10.96.0.0/12` on
// the apiserver pods.
func discoverServiceCIDR(ctx context.Context, reader client.Reader) string {
	cm := &corev1.ConfigMap{}
	if err := reader.Get(ctx, types.NamespacedName{
		Namespace: "kube-system", Name: "kubeadm-config",
	}, cm); err == nil {
		var parsed struct {
			Networking struct {
				ServiceSubnet string `json:"serviceSubnet"`
			} `json:"networking"`
		}
		if err := yaml.Unmarshal([]byte(cm.Data["ClusterConfiguration"]), &parsed); err == nil {
			if parsed.Networking.ServiceSubnet != "" {
				return parsed.Networking.ServiceSubnet
			}
		}
	}

	for _, selector := range []map[string]string{
		{"component": "kube-apiserver"},
		{"k8s-app": "kube-apiserver"},
	} {
		pods := &corev1.PodList{}
		if err := reader.List(ctx, pods,
			client.InNamespace("kube-system"), client.MatchingLabels(selector)); err != nil {
			continue
		}
		for i := range pods.Items {
			for _, container := range pods.Items[i].Spec.Containers {
				for _, arg := range append(append([]string{}, container.Command...), container.Args...) {
					if match := serviceCIDRArg.FindStringSubmatch(arg); match != nil {
						return match[1]
					}
				}
			}
		}
	}
	return ""
}

// overlayGateway is the next hop for the service network.
//
// Read rather than assumed: the overlay CIDR is an install choice, and a wrong
// next hop here produces exactly the silent failure the route exists to prevent.
func (r *ManagedNetworkReconciler) overlayGateway(ctx context.Context) (string, error) {
	subnet := &unstructured.Unstructured{}
	subnet.SetGroupVersionKind(subnetGVK)
	if err := r.Get(ctx, types.NamespacedName{Name: overlaySubnet}, subnet); err != nil {
		return "", fmt.Errorf("reading Subnet/%s: %w", overlaySubnet, err)
	}
	gateway, _, _ := unstructured.NestedString(subnet.Object, "spec", "gateway")
	if gateway == "" {
		return "", fmt.Errorf("Subnet/%s states no gateway", overlaySubnet)
	}
	return gateway, nil
}
