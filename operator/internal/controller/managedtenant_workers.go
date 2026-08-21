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
	"encoding/base64"
	"fmt"
	"os"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/yaml"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/talos"
	"github.com/mrybas/kubevirt-ui/operator/internal/tenant"
)

var (
	machineDeploymentObjectGVK = schema.GroupVersionKind{
		Group: "cluster.x-k8s.io", Version: "v1beta1", Kind: "MachineDeployment",
	}
	machineHealthCheckGVK = schema.GroupVersionKind{
		Group: "cluster.x-k8s.io", Version: "v1beta1", Kind: "MachineHealthCheck",
	}
	kubevirtMachineTemplateGVK = schema.GroupVersionKind{
		Group: "infrastructure.cluster.x-k8s.io", Version: "v1alpha1",
		Kind: "KubevirtMachineTemplate",
	}
)

const (
	// workerRootTemplate is the name of the per-worker root disk template.
	//
	// A suffix, not the disk's name: CAPK rewrites
	// `dataVolumeTemplates[].metadata.name` to `<vm>-<template>` and fixes
	// `volumes[].dataVolume.name` to match. Proved on a two-worker deployment —
	// two separate DataVolumes, each owned by its own VM.
	workerRootTemplate = "root"

	// workerReturnObserved is how long a worker took to come back from a UI
	// reboot: VMI recreated, node rejoined, tenant Ready again. Measured.
	workerReturnObserved = 3

	// nodeStartupTimeout governs a node that has never joined. Left where it
	// was measured sufficient; it is not what the window below is about.
	nodeStartupTimeout = "20m"
)

// workerUnhealthyTimeout is how long a worker may be NotReady before it is
// replaced — derived from the measurement rather than chosen.
//
// The previous five minutes left ninety seconds of margin over the observed
// return, and the health check *did* fire during an ordinary reboot: it started
// its clock and only failed to remediate because the node beat it. A slower boot
// — a larger image, a busy node, a loaded Ceph — crosses that line, and the
// operation that crosses it is a reboot.
//
// Three times the measured return, so the number moves when the measurement
// does. The cost is stated rather than hidden: a genuinely dead worker is
// detected about five minutes later. For persistent nodes that trade is
// deliberate — a false replacement re-clones a 20Gi root and churns the node's
// identity, which is dearer than waiting.
func workerUnhealthyTimeout() string {
	return fmt.Sprintf("%dm", workerReturnObserved*3)
}

// reconcileWorkers declares the worker pool: the bootstrap config each node
// boots with, the VM shape CAPK stamps out, the MachineDeployment that scales
// them, and the health check that replaces one whose node stops answering.
//
// Talos only, for now, and it says so rather than half-building a cloud-init
// tenant. The cloud-init path is a KubeadmConfigTemplate carrying a page of
// shell — a resolv.conf that survives DHCP, a disk moved under /var/lib
// because a containerDisk overlay reports zero capacity, a kubelet-config
// filter for fields a newer control plane emits — and porting it is a slice of
// its own rather than a footnote to this one.
func (r *ManagedTenantReconciler) reconcileWorkers(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace, vip, release string, addressNeeded bool,
) (ready bool, reason, message string, err error) {
	if obj.Spec.Workers.OS != "talos" {
		return false, "CloudInitNotMigrated", "this operator builds Talos " +
			"workers only; a cloud-init pool is still the product's to create", nil
	}

	bootstrap, reason, message, err := r.ensureTalosBootstrap(
		ctx, obj, namespace, vip, addressNeeded)
	if err != nil || bootstrap == "" {
		return false, reason, message, err
	}
	if err := r.ensureMachineTemplate(ctx, obj, namespace, release); err != nil {
		return false, "", "", err
	}
	if err := r.ensureMachineDeployment(ctx, obj, namespace, bootstrap); err != nil {
		return false, "", "", err
	}
	if err := r.ensureMachineHealthCheck(ctx, obj, namespace); err != nil {
		return false, "", "", err
	}

	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(machineDeploymentObjectGVK)
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: obj.Name + "-workers",
	}, live); err != nil {
		return false, "", "", fmt.Errorf("reading the MachineDeployment: %w", err)
	}
	wanted := int64(obj.Spec.Workers.Count)
	ready64, _, _ := unstructured.NestedInt64(live.Object, "status", "readyReplicas")
	if ready64 >= wanted {
		return true, "Ready", fmt.Sprintf("%d/%d workers", ready64, wanted), nil
	}
	return false, "Joining", fmt.Sprintf("%d/%d workers", ready64, wanted), nil
}

// ensureTalosBootstrap writes the machine config a worker boots with, and
// returns the name of the template holding it.
//
// **It waits for both CAs**, and the waiting is the design rather than an
// inconvenience. The template is immutable: whatever is written is what every
// worker of this tenant gets for as long as it lives. Kamaji mints the
// Kubernetes CA while this is assembling the config, and the two used to race —
// measured to the second on 2026-08-19, a read at 09:41:30 that missed a secret
// created at 09:41:31, and a tenant that then failed every boot with
// `secrets.KubeletController: missing accepted Kubernetes CAs` twelve seconds
// in, before any network, so it read as a mystery rather than a race.
//
// The product waited inside the creating request for up to two minutes. Here
// there is nothing to wait in: the tenant reports what is missing and comes
// back when it exists.
func (r *ManagedTenantReconciler) ensureTalosBootstrap(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace, vip string, addressNeeded bool,
) (name, reason, message string, err error) {
	secrets := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: obj.Name + "-talos-secrets",
	}, secrets); err != nil {
		if apierrors.IsNotFound(err) {
			return "", "WaitingForSecrets",
				"waiting for the tenant's machine secrets", nil
		}
		return "", "", "", fmt.Errorf("reading the machine secrets: %w", err)
	}

	if r.resolverPending(ctx, obj) {
		return "", "WaitingForResolver", fmt.Sprintf(
			"no resolver for network %q yet — neither its own status nor "+
				"kube-ovn's vpc-dns-config names one. A worker in a VPC cannot "+
				"reach the public resolvers before egress exists, and this "+
				"config is written once into an immutable template",
			obj.Spec.Network), nil
	}

	talosCA, err := r.certificateFrom(ctx, namespace, obj.Name+"-talos-ca", "tls.crt")
	if err != nil {
		return "", "", "", err
	}
	if talosCA == "" {
		// Talos refuses a config with neither, and says so before it dials
		// anything: "issuing CA or some accepted CAs are required".
		return "", "WaitingForTalosCA", fmt.Sprintf(
			"waiting for cert-manager to issue %s/%s-talos-ca", namespace, obj.Name), nil
	}
	k8sCA, err := r.certificateFrom(ctx, namespace, obj.Name+"-ca", "ca.crt")
	if err != nil {
		return "", "", "", err
	}
	if k8sCA == "" {
		return "", "WaitingForKubernetesCA", fmt.Sprintf(
			"waiting for Kamaji to mint %s/%s-ca. Writing the config without it "+
				"would bake the defect in permanently — the template is immutable "+
				"and the node fails every boot with 'missing accepted Kubernetes "+
				"CAs'", namespace, obj.Name), nil
	}

	config := r.talosWorkerConfig(ctx, obj, namespace, vip, addressNeeded,
		string(secrets.Data["machine.token"]),
		string(secrets.Data["cluster.id"]),
		string(secrets.Data["cluster.secret"]),
		talosCA, k8sCA)
	rendered, err := yaml.Marshal(config)
	if err != nil {
		return "", "", "", fmt.Errorf("rendering the worker config: %w", err)
	}

	name = obj.Name + "-workers"
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(talosConfigTemplateGVK)
	err = r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, live)
	if err == nil {
		// Immutable, and left alone on purpose. Rewriting it is not possible,
		// and replacing it would roll every worker of a running tenant.
		return name, "", "", nil
	}
	if !apierrors.IsNotFound(err) {
		return "", "", "", fmt.Errorf("reading the worker template: %w", err)
	}

	created := &unstructured.Unstructured{}
	created.SetGroupVersionKind(talosConfigTemplateGVK)
	created.SetName(name)
	created.SetNamespace(namespace)
	created.SetLabels(map[string]string{
		"kubevirt-ui.io/managed": "true",
		"kubevirt-ui.io/tenant":  obj.Name,
	})
	if err := unstructured.SetNestedMap(created.Object, map[string]any{
		// `none` on purpose: every value here is already known, and several
		// have to match objects created elsewhere exactly. Letting the provider
		// generate the config would mean reconciling two sources of truth.
		"generateType": "none",
		"data":         string(rendered),
	}, "spec", "template", "spec"); err != nil {
		return "", "", "", err
	}
	if err := r.Create(ctx, created); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return name, "", "", nil
		}
		return "", "", "", fmt.Errorf("creating %s/%s: %w", namespace, name, err)
	}
	kube.CountWrite(r.Scheme, created, tenantControllerName, "created")
	return name, "", "", nil
}

// certificateFrom reads one base64 field out of a Secret, or "" when the Secret
// is not there yet. Kubernetes stores these already encoded, so the value
// passes through untouched — which is the form Talos wants.
func (r *ManagedTenantReconciler) certificateFrom(
	ctx context.Context, namespace, name, key string,
) (string, error) {
	secret := &corev1.Secret{}
	err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, secret)
	if apierrors.IsNotFound(err) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("reading %s/%s: %w", namespace, name, err)
	}
	// client-go has already decoded the wire form; Talos wants it encoded.
	return base64.StdEncoding.EncodeToString(secret.Data[key]), nil
}

// talosWorkerConfig is the machine config a worker boots with.
//
// Three settings carry the design:
//
//   - the endpoint is the tenant's **own address** when it has one, and a name
//     otherwise. The name is what produces SNI, and SNI is only needed while
//     tenants share one listener on 50001. A VPC worker can use nothing but the
//     address: the name resolves through cluster DNS to a ClusterIP, and an
//     isolated VPC has neither;
//   - `extraHostEntries` pins that name to the control plane inside the node,
//     so joining needs no working DNS — which matters, because the node has
//     none until it has joined;
//   - `kubePrism` is off. It proxies the apiserver via localhost, which would
//     bypass the name and take the SNI with it.
func (r *ManagedTenantReconciler) talosWorkerConfig(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace, vip string, addressNeeded bool,
	machineToken, clusterID, clusterSecret, talosCA, k8sCA string,
) map[string]any {
	endpointHost := talosServiceName(obj.Name) + "." + namespace + ".svc"
	pinned := vip
	if !addressNeeded {
		// On the default overlay the name resolves and the control plane is a
		// ClusterIP; there is no address to pin, and nothing to pin it for.
		pinned = ""
	} else {
		endpointHost = vip
	}

	machineNetwork := map[string]any{}
	if pinned != "" {
		machineNetwork["extraHostEntries"] = []any{map[string]any{
			"ip":      pinned,
			"aliases": toAny(signerDNSNames(obj.Name, namespace)),
		}}
	}
	// Measured, not assumed: with bridge binding KubeVirt runs the guest's DHCP
	// itself and hands over the launcher pod's resolv.conf — the host cluster's
	// resolver, which a VPC cannot reach. kube-ovn's own subnet DHCP option
	// never arrives. The node then resolves nothing, NTP fails, and Talos parks
	// the kubelet on "waiting for time sync" with no error naming DNS.
	if resolvers := r.workerNameservers(ctx, obj); len(resolvers) > 0 {
		machineNetwork["nameservers"] = toAny(resolvers)
	}

	machine := map[string]any{
		"type":     "worker",
		"token":    machineToken,
		"network":  machineNetwork,
		"features": map[string]any{"kubePrism": map[string]any{"enabled": false}},
		"kubelet": map[string]any{
			// Without rotation the kubelet's client certificate expires and the
			// node silently stops being able to talk to the API.
			"extraArgs": map[string]any{"rotate-certificates": "true"},
			// Pinned to the tenant's Kubernetes version rather than left to the
			// image. Talos ships whatever kubelet matches its own release —
			// against an older control plane that is several minors of skew,
			// and the node boots, reports healthy, and never registers.
			"image": "ghcr.io/siderolabs/kubelet:" + obj.Spec.KubernetesVersion,
		},
		// Talos refuses a config with neither, before it dials anything. A
		// worker issues no certificates — it asks the signer for one — so the
		// CA goes in without its key. acceptedCAs is what modern Talos reads;
		// ca is kept for older releases.
		"ca":          map[string]any{"crt": talosCA},
		"acceptedCAs": []any{map[string]any{"crt": talosCA}},
	}
	if servers := r.workerTimeServers(vip); len(servers) > 0 {
		// Talos will not start the kubelet against an unsynchronised clock, and
		// says so in a way that reads as a network fault. With the public pool
		// alone, joining becomes a soft dependency on the internet — the one
		// property the routed-egress design exists to remove.
		machine["time"] = map[string]any{"servers": toAny(servers)}
	}

	return map[string]any{
		"version": "v1alpha1",
		"machine": machine,
		"cluster": map[string]any{
			"id":     clusterID,
			"secret": clusterSecret,
			// A different field from machine.token above: that one
			// authenticates the machine to trustd, this is what Talos writes
			// into the kubelet's bootstrap kubeconfig. Setting only the first
			// leaves the kubelet with nothing to present, and it exits in a
			// loop while the node never files a CSR. Same value on purpose.
			"token": machineToken,
			"controlPlane": map[string]any{
				"endpoint": fmt.Sprintf("https://%s:%d", endpointHost, tenantAPIPort),
			},
			"network": map[string]any{
				"podSubnets":     []any{obj.Spec.PodCIDR},
				"serviceSubnets": []any{obj.Spec.ServiceCIDR},
			},
			// The Kubernetes CA, and a different one from the machine CA above.
			// Without it Talos accepts the config, starts, and never brings the
			// kubelet up: "missing accepted Kubernetes CAs".
			"ca":          map[string]any{"crt": k8sCA},
			"acceptedCAs": []any{map[string]any{"crt": k8sCA}},
			"discovery": map[string]any{
				// Both registries off: the Kubernetes one needs credentials the
				// node does not have yet, and the service one talks to an
				// external endpoint a tenant network may not reach.
				"enabled": true,
				"registries": map[string]any{
					"kubernetes": map[string]any{"disabled": true},
					"service":    map[string]any{"disabled": true},
				},
			},
		},
	}
}

func toAny(values []string) []any {
	out := make([]any, 0, len(values))
	for _, value := range values {
		out = append(out, value)
	}
	return out
}

// workerNameservers are the resolvers a Talos worker boots with.
//
// A worker in a VPC must use the VpcDns address first: the public resolvers are
// only reachable once the VPC has egress, and a tenant is normally created
// before any gateway is attached to it. That address resolves public names too
// — it forwards to the cluster's resolver — so it covers image pulls as well as
// anything in-fabric.
//
// Not yet carried over: the per-tenant DNS overrides the product's request
// object has (`dns_mode`, `dns_servers`). The CRD has no field for them, and
// inventing one here would be a second place to configure the same thing.
func (r *ManagedTenantReconciler) workerNameservers(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
) []string {
	public := []string{"1.1.1.1", "8.8.8.8"}
	if obj.Spec.Network == "" {
		return public
	}
	// Read off the network the tenant is in, which already resolved it from
	// kube-ovn's own configuration and published it. One fact in one place: an
	// environment variable here would be a second, and the two would agree
	// until they did not.
	if vip := r.vpcResolver(ctx, obj); vip != "" {
		return append([]string{vip}, public...)
	}
	return public
}

// vpcResolver is the address a worker in this tenant's VPC resolves through.
//
// Two sources, in order, and neither is an environment variable: the network
// object if this VPC is described by one — it has already resolved the address
// and published it — and otherwise kube-ovn's own `vpc-dns-config`, which is
// where the cluster states it and where the network controller reads it from
// too. A VPC built by the product has no ManagedNetwork, and refusing to build
// workers in one would be this operator insisting the world be its own shape.
//
// Read straight from the API server. The value is written into an immutable
// template — read it a beat early from a cache that has not caught up, and the
// worker resolves nothing for the rest of its life.
func (r *ManagedTenantReconciler) vpcResolver(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
) string {
	network := &platformv1alpha1.ManagedNetwork{}
	if err := r.reader().Get(ctx, types.NamespacedName{
		Name: obj.Spec.Network,
	}, network); err == nil && network.Status.DNSServer != "" {
		return network.Status.DNSServer
	}

	namespace := r.KubeOVNNamespace
	if namespace == "" {
		namespace = os.Getenv("KUBE_OVN_NAMESPACE")
	}
	if namespace == "" {
		return ""
	}
	config := &corev1.ConfigMap{}
	if err := r.reader().Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: vpcDNSConfigMap,
	}, config); err != nil {
		return ""
	}
	return strings.TrimSpace(config.Data["coredns-vip"])
}

// resolverPending is whether this tenant's network has not yet said which
// resolver its workers should use.
//
// The same shape as the CA wait, and for the same reason: the config is written
// once into an immutable template. A tenant created moments after its network
// would otherwise be given the public resolvers permanently — which in an
// isolated VPC means a node that resolves nothing until somebody notices.
func (r *ManagedTenantReconciler) resolverPending(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
) bool {
	if obj.Spec.Network == "" {
		return false
	}
	return r.vpcResolver(ctx, obj) == ""
}

// workerTimeServers put the tenant's own address first, because it is the one
// that works with no egress at all. The public servers stay behind it for
// deployments where egress is never in question.
func (r *ManagedTenantReconciler) workerTimeServers(vip string) []string {
	public := []string{"time.cloudflare.com", "pool.ntp.org"}
	if vip == "" {
		return public
	}
	return append([]string{vip}, public...)
}

// ensureMachineTemplate is the VM shape CAPK stamps a worker out of.
func (r *ManagedTenantReconciler) ensureMachineTemplate(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace, release string,
) error {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(kubevirtMachineTemplateGVK)
	live.SetName(obj.Name + "-workers")
	live.SetNamespace(namespace)
	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		mergeLabels(live, map[string]string{"kubevirt-ui.io/tenant": obj.Name})

		podAnnotations := map[string]any{
			// Bridge binding needs this to permit live migration on a
			// pod-network bridge interface.
			"kubevirt.io/allow-pod-bridge-network-live-migration": "true",
		}
		checkStrategy := "ssh"
		if obj.Spec.Network != "" {
			// Pin the launcher pod into the VPC's subnet. CAPK, which lives on
			// the default overlay, then cannot SSH into the worker — so the
			// default bootstrap check would fail forever and the deployment
			// would read zero-ready with healthy nodes. `none` skips only that
			// verification; the machine config still runs.
			podAnnotations["ovn.kubernetes.io/logical_switch"] = obj.Spec.Network + "-default"
			checkStrategy = "none"
		}

		vmSpec := map[string]any{
			"runStrategy": "Always",
			// One root disk per worker, cloned from the shared golden. This
			// used to point every worker at the golden PVC by name, and the
			// consequences were measured rather than feared: with one worker
			// the golden stops being golden, because the node writes into it;
			// with two it is two writers on one block device; and deleting
			// worker A takes the DataVolume with it — blockOwnerDeletion —
			// which silently wipes worker B's root during a rolling update.
			"dataVolumeTemplates": []any{r.workerRootDisk(obj, release)},
			"template": map[string]any{
				"metadata": map[string]any{
					"labels": map[string]any{
						"kubevirt-ui.io/tenant":      obj.Name,
						"kubevirt-ui.io/folder":      obj.Spec.Folder,
						"kubevirt-ui.io/environment": obj.Spec.Environment,
					},
					"annotations": podAnnotations,
				},
				"spec": map[string]any{
					"domain": map[string]any{
						"cpu":    map[string]any{"cores": int64(obj.Spec.Workers.VCPU)},
						"memory": map[string]any{"guest": obj.Spec.Workers.Memory},
						"devices": map[string]any{
							"networkInterfaceMultiqueue": true,
							"interfaces": []any{map[string]any{
								"name": "default", "bridge": map[string]any{},
							}},
							"disks": []any{
								map[string]any{"name": "root",
									"disk": map[string]any{"bus": "virtio"}},
								map[string]any{"name": "data",
									"disk": map[string]any{"bus": "virtio"}},
							},
						},
					},
					"networks": []any{map[string]any{
						"name": "default", "pod": map[string]any{},
					}},
					"evictionStrategy": "External",
					"volumes": []any{
						map[string]any{"name": "root",
							"dataVolume": map[string]any{"name": workerRootTemplate}},
						map[string]any{"name": "data", "emptyDisk": map[string]any{
							"capacity": obj.Spec.Workers.Disk,
						}},
					},
				},
			},
		}

		return unstructured.SetNestedMap(live.Object, map[string]any{
			"virtualMachineBootstrapCheck": map[string]any{
				"checkStrategy": checkStrategy,
			},
			"virtualMachineTemplate": map[string]any{"spec": vmSpec},
		}, "spec", "template", "spec")
	})
	if err != nil {
		return fmt.Errorf("declaring the machine template: %w", err)
	}
	return nil
}

// workerRootDisk is the per-worker clone of the shared golden.
//
// `source.pvc` with `storage` — not `pvc` — so CDI takes the clone strategy
// from the target's storage profile rather than being told one the backend may
// not support. The size follows the golden unless the tenant asked for more: a
// clone smaller than its source is refused at admission, which would fail every
// worker with an error nobody would connect to a disk-size field.
func (r *ManagedTenantReconciler) workerRootDisk(
	obj *platformv1alpha1.ManagedTenant, release string,
) map[string]any {
	size := tenant.LargerSize(obj.Spec.Workers.Disk, goldenSize)
	storage := map[string]any{
		"resources": map[string]any{"requests": map[string]any{"storage": size}},
	}
	// The tenant's class, not the golden's. The golden is meant for an
	// erasure-coded pool — read-only reference data cloned many times — and the
	// clones want replica. Measured: the live tenants' roots are on
	// `ceph-block`, and the first version of this sent them wherever the golden
	// lives.
	if class := obj.Spec.Storage.ClassName; class != "" {
		storage["storageClassName"] = class
	}
	return map[string]any{
		"metadata": map[string]any{
			"name": workerRootTemplate,
			"labels": map[string]any{
				"kubevirt-ui.io/managed":     "true",
				"kubevirt-ui.io/tenant":      obj.Name,
				"kubevirt-ui.io/worker-root": "true",
			},
		},
		"spec": map[string]any{
			// The shared golden, in its own namespace. Crossing that boundary
			// is what CDI gates on `datavolumes/source`, and it is the whole
			// point: one import per Talos release instead of one per tenant.
			"source": map[string]any{"pvc": map[string]any{
				"name":      talos.GoldenName(release),
				"namespace": goldenNamespace(),
			}},
			"storage": storage,
		},
	}
}

// ensureMachineDeployment scales the pool.
func (r *ManagedTenantReconciler) ensureMachineDeployment(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace, bootstrap string,
) error {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(machineDeploymentObjectGVK)
	live.SetName(obj.Name + "-workers")
	live.SetNamespace(namespace)
	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		mergeLabels(live, map[string]string{"kubevirt-ui.io/tenant": obj.Name})
		return unstructured.SetNestedMap(live.Object, map[string]any{
			"clusterName": obj.Name,
			"replicas":    int64(obj.Spec.Workers.Count),
			"selector":    map[string]any{"matchLabels": map[string]any{}},
			"template": map[string]any{"spec": map[string]any{
				"clusterName": obj.Name,
				// Remediating a dead worker means deleting its Machine, and
				// CAPI drains the node first. A node that is gone cannot be
				// drained: it stopped on a disruption budget that needed a
				// healthy pod it no longer had, and with no timeout CAPI
				// retried forever — the replacement never arrived and the
				// tenant kept a NotReady node indefinitely.
				"nodeDrainTimeout": "5m",
				"version":          obj.Spec.KubernetesVersion,
				// Talos nodes are not bootstrapped by kubeadm: they get a
				// machine config and ask a signer for a certificate, which is a
				// different provider entirely.
				"bootstrap": map[string]any{"configRef": map[string]any{
					"apiVersion": talosConfigTemplateGVK.GroupVersion().String(),
					"kind":       talosConfigTemplateGVK.Kind,
					"name":       bootstrap,
				}},
				"infrastructureRef": map[string]any{
					"apiVersion": kubevirtMachineTemplateGVK.GroupVersion().String(),
					"kind":       kubevirtMachineTemplateGVK.Kind,
					"name":       obj.Name + "-workers",
				},
			}},
		}, "spec")
	})
	if err != nil {
		return fmt.Errorf("declaring the MachineDeployment: %w", err)
	}
	return nil
}

// ensureMachineHealthCheck replaces a worker whose node stops being Ready.
//
// Without it nothing notices. Killing a worker's VMI brought the VM straight
// back — same name, fresh disk, no kubelet configuration on it — and the tenant
// kept a node that never returned, while CAPI reported the Machine healthy
// because the infrastructure existed. Only the *node* was gone.
//
// `maxUnhealthy: 100%` on purpose: a one-worker tenant is the common case here,
// and the usual guard would refuse to remediate the only worker — exactly when
// the tenant is fully down and most needs it.
func (r *ManagedTenantReconciler) ensureMachineHealthCheck(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) error {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(machineHealthCheckGVK)
	live.SetName(obj.Name + "-workers")
	live.SetNamespace(namespace)
	timeout := workerUnhealthyTimeout()
	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		mergeLabels(live, map[string]string{"kubevirt-ui.io/tenant": obj.Name})
		return unstructured.SetNestedMap(live.Object, map[string]any{
			"clusterName":        obj.Name,
			"maxUnhealthy":       "100%",
			"nodeStartupTimeout": nodeStartupTimeout,
			"selector": map[string]any{"matchLabels": map[string]any{
				"cluster.x-k8s.io/deployment-name": obj.Name + "-workers",
			}},
			"unhealthyConditions": []any{
				map[string]any{"type": "Ready", "status": "False", "timeout": timeout},
				map[string]any{"type": "Ready", "status": "Unknown", "timeout": timeout},
			},
		}, "spec")
	})
	if err != nil {
		return fmt.Errorf("declaring the health check: %w", err)
	}
	return nil
}

func workersCondition(ready bool, reason, message string) metav1.Condition {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	if reason == "" {
		reason = "Pending"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionWorkersReady,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}
