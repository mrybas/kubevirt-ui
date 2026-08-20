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
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"math/big"
	"os"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

var (
	clusterGVK = schema.GroupVersionKind{
		Group: "cluster.x-k8s.io", Version: "v1beta1", Kind: "Cluster",
	}
	kamajiControlPlaneGVK = schema.GroupVersionKind{
		Group: "controlplane.cluster.x-k8s.io", Version: "v1alpha1",
		Kind: "KamajiControlPlane",
	}
	kubevirtClusterGVK = schema.GroupVersionKind{
		Group: "infrastructure.cluster.x-k8s.io", Version: "v1alpha1",
		Kind: "KubevirtCluster",
	}
)

const (
	signerImageEnv    = "TALOS_SIGNER_IMAGE"
	konnProxyImageEnv = "TENANTS_KONNECTIVITY_PROXY_IMAGE"
	konnAgentImageEnv = "TENANTS_KONNECTIVITY_AGENT_IMAGE"

	defaultKonnProxyImage = "registry.k8s.io/kas-network-proxy/proxy-server"
	defaultKonnAgentImage = "registry.k8s.io/kas-network-proxy/proxy-agent"

	// Pinned by digest, not by tag: the signer is on the join path, and a tag
	// that moves under a tenant is a fleet of workers that cannot get a
	// certificate for a reason nothing in the cluster records.
	defaultSignerImage = "ghcr.io/clastix/talos-csr-signer" +
		"@sha256:827b62b5fc2859d66f06f5c1f8d2473ab7109d0600d551269d8ddb98e4a39a18"
)

// clusterObject is an empty CAPI Cluster, for the watch. Unstructured because
// this operator does not vendor CAPI's types: it writes four foreign kinds and
// carrying their Go modules to name one field of each is a dependency the
// migration does not need.
func clusterObject() *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(clusterGVK)
	return obj
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// reconcileControlPlane declares the tenant's control plane: the CAPI Cluster,
// the infrastructure object CAPK reads, and the Kamaji control plane that
// actually runs an apiserver.
//
// Declares, not builds. Kamaji creates the pods and CAPI wires them together;
// what this owns is the description they act on, which is why every write here
// is write-on-diff and none of it waits for anything.
func (r *ManagedTenantReconciler) reconcileControlPlane(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace, vip string, addressNeeded bool,
) (ready bool, reason, message string, err error) {
	if obj.Spec.Workers.OS == "talos" {
		if err := r.ensureTalosMachineSecrets(ctx, obj, namespace); err != nil {
			return false, "", "", err
		}
	}

	if err := r.ensureKubevirtCluster(ctx, obj, namespace); err != nil {
		return false, "", "", err
	}
	if err := r.ensureKamajiControlPlane(ctx, obj, namespace, vip, addressNeeded); err != nil {
		return false, "", "", err
	}

	host, port, reason, message := r.controlPlaneEndpoint(ctx, obj, namespace, vip, addressNeeded)
	if host == "" {
		return false, reason, message, nil
	}
	if err := r.ensureCluster(ctx, obj, namespace, host, port, addressNeeded); err != nil {
		return false, "", "", err
	}

	// Readiness is CAPI's answer, not ours: it flips controlPlaneReady when the
	// control plane it was told about actually answers.
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(clusterGVK)
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: obj.Name,
	}, live); err != nil {
		return false, "", "", fmt.Errorf("reading the Cluster: %w", err)
	}
	if cpReady, _, _ := unstructured.NestedBool(live.Object, "status", "controlPlaneReady"); cpReady {
		return true, "Ready", fmt.Sprintf("the control plane answers on %s:%d", host, port), nil
	}
	return false, "Provisioning", fmt.Sprintf(
		"declared at %s:%d; waiting for Kamaji to bring it up", host, port), nil
}

// controlPlaneEndpoint is the address a worker will be told to join.
//
// Two models, and which one applies is decided by whether the tenant has a
// network of its own:
//
//   - **In a VPC**: its own address, and the api port on it. cluster-info then
//     carries `<vip>:6443`, which is reachable from an isolated VPC. The
//     endpoint used to be the external ingress name here, on the assumption
//     that cluster-info already carried the address — it cannot, because CAPI
//     copies this field into the worker's discovery endpoint and the worker has
//     to fetch cluster-info before it can learn anything from it. So the worker
//     dialled a name its VPC could neither resolve nor route to, and three lab
//     runs read that as "CAPK will not bootstrap".
//   - **On the default overlay**: the Kamaji Service's ClusterIP, natively
//     routable there. It does not exist when the Cluster is first written, so
//     the tenant says so and comes back — the product patched the field after
//     polling for the Service, which is the same thing done in a place where
//     waiting costs a request handler.
func (r *ManagedTenantReconciler) controlPlaneEndpoint(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace, vip string, addressNeeded bool,
) (host string, port int64, reason, message string) {
	if addressNeeded {
		if vip == "" {
			return "", 0, "WaitingForAddress",
				"waiting for the tenant's address: it is what the workers join"
		}
		return vip, tenantAPIPort, "", ""
	}

	service := &corev1.Service{}
	err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: obj.Name}, service)
	if apierrors.IsNotFound(err) {
		return "", 0, "WaitingForControlPlaneService", fmt.Sprintf(
			"waiting for Kamaji to create %s/%s, whose ClusterIP the workers join",
			namespace, obj.Name)
	}
	if err != nil || service.Spec.ClusterIP == "" || service.Spec.ClusterIP == "None" {
		return "", 0, "WaitingForControlPlaneService", fmt.Sprintf(
			"%s/%s has no ClusterIP yet", namespace, obj.Name)
	}
	return service.Spec.ClusterIP, tenantAPIPort, "", ""
}

// ensureCluster writes the CAPI Cluster: the object that ties the control plane
// to the infrastructure and tells every worker where to join.
func (r *ManagedTenantReconciler) ensureCluster(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace, host string, port int64, inVPC bool,
) error {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(clusterGVK)
	live.SetName(obj.Name)
	live.SetNamespace(namespace)
	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		labels := map[string]string{"kubevirt-ui.io/tenant": obj.Name}
		if obj.Spec.Folder != "" {
			labels["kubevirt-ui.io/folder"] = obj.Spec.Folder
		}
		if obj.Spec.Environment != "" {
			labels["kubevirt-ui.io/environment"] = obj.Spec.Environment
		}
		live.SetLabels(labels)
		live.SetAnnotations(map[string]string{
			"kubevirt-ui.io/display-name": obj.Spec.DisplayName,
		})

		network := map[string]any{
			"pods":     map[string]any{"cidrBlocks": []any{obj.Spec.PodCIDR}},
			"services": map[string]any{"cidrBlocks": []any{obj.Spec.ServiceCIDR}},
		}
		if inVPC {
			// apiServerPort flows to the apiserver's --secure-port and to the
			// port advertised in cluster-info.
			network["apiServerPort"] = port
		}
		return unstructured.SetNestedMap(live.Object, map[string]any{
			"controlPlaneEndpoint": map[string]any{"host": host, "port": port},
			"clusterNetwork":       network,
			// Same namespace, and it must stay that way: the CAPI webhook
			// rejects a controlPlaneRef that points anywhere else.
			"controlPlaneRef": map[string]any{
				"apiVersion": kamajiControlPlaneGVK.GroupVersion().String(),
				"kind":       kamajiControlPlaneGVK.Kind,
				"name":       obj.Name,
			},
			"infrastructureRef": map[string]any{
				"apiVersion": kubevirtClusterGVK.GroupVersion().String(),
				"kind":       kubevirtClusterGVK.Kind,
				"name":       obj.Name,
			},
		}, "spec")
	})
	if err != nil {
		return fmt.Errorf("declaring the Cluster %s/%s: %w", namespace, obj.Name, err)
	}
	return nil
}

// ensureKubevirtCluster writes what CAPK reads.
//
// Its spec has exactly four fields and none of them is a storage class: the API
// server drops unknown fields silently, so writing one would be a no-op that
// reads like configuration. The annotation is the load-bearing part — it tells
// CAPK the control plane is somebody else's, which stops it creating a
// `<name>-lb` Service with selectors that match nothing.
func (r *ManagedTenantReconciler) ensureKubevirtCluster(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) error {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(kubevirtClusterGVK)
	live.SetName(obj.Name)
	live.SetNamespace(namespace)
	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		live.SetLabels(map[string]string{"kubevirt-ui.io/tenant": obj.Name})
		annotations := live.GetAnnotations()
		if annotations == nil {
			annotations = map[string]string{}
		}
		annotations["cluster.x-k8s.io/managed-by"] = "kamaji"
		live.SetAnnotations(annotations)
		return nil
	})
	if err != nil {
		return fmt.Errorf("declaring the KubevirtCluster %s/%s: %w",
			namespace, obj.Name, err)
	}
	return nil
}

// ensureKamajiControlPlane writes the control plane itself.
func (r *ManagedTenantReconciler) ensureKamajiControlPlane(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace, vip string, inVPC bool,
) error {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(kamajiControlPlaneGVK)
	live.SetName(obj.Name)
	live.SetNamespace(namespace)
	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		live.SetLabels(map[string]string{"kubevirt-ui.io/tenant": obj.Name})

		network := map[string]any{
			// ClusterIP, deliberately. `serviceAddress` is a NodePort/VIP-only
			// field and useless here, and the address the workers use is
			// advertised instead.
			"serviceType": "ClusterIP",
			"certSANs":    []any{},
		}
		var sans []any
		if host := r.ingressHost(obj.Name); host != "" {
			sans = append(sans, host)
		}

		deployment := map[string]any{
			"podAdditionalMetadata": map[string]any{
				"labels": map[string]any{
					"cluster.x-k8s.io/cluster-name": obj.Name,
					"cluster.x-k8s.io/role":         "control-plane",
				},
			},
		}

		if obj.Spec.Workers.OS == "talos" {
			// A Talos worker dials a CSR signer beside the apiserver, so the
			// control plane grows a sidecar, the secrets it reads, and the
			// names it answers to — which must be in certSANs or the join
			// fails TLS before trustd is reached at all.
			//
			// `extraContainers`/`extraVolumes` are what KamajiControlPlane
			// calls these. TenantControlPlane calls the same things
			// `additionalContainers`/`additionalVolumes`, and writing those
			// names here is not an error anyone reports: unknown fields are
			// pruned silently, the object applies, the tenant comes up Ready,
			// and the signer simply is not there — while the worker waits for a
			// certificate nothing will ever issue.
			deployment["extraContainers"] = []any{signerSidecar(obj.Name)}
			deployment["extraVolumes"] = signerVolumes(obj.Name)
			for _, name := range signerDNSNames(obj.Name, namespace) {
				sans = append(sans, name)
			}
			// No `network.additionalPorts` here, though the product writes
			// one: **the field does not exist.** KamajiControlPlane's schema
			// has advertiseAddress, certSANs, dnsServiceIPs, gateway, ingress,
			// loadBalancerConfig, serviceAddress, serviceAnnotations,
			// serviceLabels and serviceType — and nothing else, so the API
			// server prunes it in silence. Verified on the two live tenants:
			// their network carries exactly advertiseAddress, certSANs and
			// serviceType.
			//
			// It never mattered because trustd is published by the tenant's own
			// Services — the LoadBalancer on its address and the in-cluster one
			// beside it — which is where the workers actually reach it.
		}
		network["certSANs"] = sans
		if inVPC && vip != "" {
			// cluster-info then points workers at <vip>:<apiServerPort>, which
			// is the only address an isolated VPC can reach.
			network["advertiseAddress"] = vip
		}

		spec := map[string]any{
			"replicas":      int64(obj.Spec.ControlPlaneReplicas),
			"version":       obj.Spec.KubernetesVersion,
			"dataStoreName": "default",
			"addons": map[string]any{
				"coreDNS":   map[string]any{},
				"kubeProxy": map[string]any{},
				"konnectivity": map[string]any{
					"server": map[string]any{
						"port":  int64(tenantKonnPort),
						"image": envOr(konnProxyImageEnv, defaultKonnProxyImage),
						"resources": map[string]any{
							"requests": map[string]any{"cpu": "50m", "memory": "64Mi"},
						},
					},
					"agent": map[string]any{
						"image": envOr(konnAgentImageEnv, defaultKonnAgentImage),
					},
				},
			},
			"kubelet": map[string]any{
				"cgroupfs":              "systemd",
				"preferredAddressTypes": []any{"InternalIP", "ExternalIP"},
			},
			"network":    network,
			"deployment": deployment,
		}
		// Only when the tenant asked for it *and* the platform has a provider
		// to point at. A tenant that opted out runs with no `--oidc-*` flags at
		// all, which is what a deployment whose provider the apiserver cannot
		// reach actually needs.
		if args := oidcArgs(obj); len(args) > 0 {
			spec["apiServer"] = map[string]any{"extraArgs": args}
		}
		return unstructured.SetNestedMap(live.Object, spec, "spec")
	})
	if err != nil {
		return fmt.Errorf("declaring the KamajiControlPlane %s/%s: %w",
			namespace, obj.Name, err)
	}
	return nil
}

// oidcArgs are the apiserver flags that make the platform's identity provider
// usable inside a tenant.
//
// Gated on the issuer being https: an apiserver told to trust an http issuer
// refuses to start, and a control plane that will not start is a worse answer
// than one without single sign-on.
func oidcArgs(obj *platformv1alpha1.ManagedTenant) []any {
	if !obj.Spec.EnableOIDC {
		return nil
	}
	issuer := envOr("OIDC_ISSUER", "")
	if !strings.HasPrefix(issuer, "https://") {
		return nil
	}
	return []any{
		"--oidc-issuer-url=" + issuer,
		"--oidc-client-id=" + envOr("OIDC_CLIENT_ID", "kubevirt-ui"),
		"--oidc-username-claim=email",
		"--oidc-groups-claim=groups",
	}
}

// ingressHost is the external name for this tenant's apiserver, when the
// deployment has an ingress domain at all. Empty means no such name exists, and
// putting one in the certificate would be inventing it.
func (r *ManagedTenantReconciler) ingressHost(name string) string {
	if domain := envOr("TENANTS_INGRESS_DOMAIN", ""); domain != "" {
		return name + "." + domain
	}
	return ""
}

// signerSidecar is the talos-csr-signer container that runs beside the
// apiserver.
//
// The flag names are the image's, and getting one wrong is not subtle: the
// binary exits with `unknown flag` and crash-loops while the tenant stays Ready
// and the worker waits for a certificate forever.
//
// Three secrets, not one. The server certificate proves the signer's identity
// to the worker; the CA **key** is what signs the CSR, so mounting only the CA
// certificate leaves it able to issue nothing; and the machine token is how a
// worker proves it belongs to this tenant at all.
func signerSidecar(name string) map[string]any {
	return map[string]any{
		"name":  "talos-csr-signer",
		"image": envOr(signerImageEnv, defaultSignerImage),
		"args": []any{
			fmt.Sprintf("--port=%d", tenantTrustdPort),
			"--tls-cert-path=/etc/talos-signer/tls.crt",
			"--tls-key-path=/etc/talos-signer/tls.key",
			"--ca-cert-path=/etc/talos-ca/tls.crt",
			"--ca-key-path=/etc/talos-ca/tls.key",
		},
		"env": []any{map[string]any{
			"name": "TALOS_TOKEN",
			"valueFrom": map[string]any{"secretKeyRef": map[string]any{
				"name": name + "-talos-secrets", "key": "machine.token",
			}},
		}},
		"ports": []any{map[string]any{
			"name": "trustd", "containerPort": int64(tenantTrustdPort),
		}},
		"volumeMounts": []any{
			map[string]any{"name": "talos-signer-certs",
				"mountPath": "/etc/talos-signer", "readOnly": true},
			map[string]any{"name": "talos-ca-certs",
				"mountPath": "/etc/talos-ca", "readOnly": true},
		},
		"resources": map[string]any{
			"requests": map[string]any{"cpu": "10m", "memory": "32Mi"},
			"limits":   map[string]any{"memory": "128Mi"},
		},
	}
}

func signerVolumes(name string) []any {
	return []any{
		map[string]any{"name": "talos-signer-certs",
			"secret": map[string]any{"secretName": name + "-talos-signer"}},
		map[string]any{"name": "talos-ca-certs",
			"secret": map[string]any{"secretName": name + "-talos-ca"}},
	}
}

// ensureTalosMachineSecrets writes the values every worker of this tenant is
// derived from — and writes them **once**.
//
// Create-if-absent, never overwritten, and that is the whole contract.
// Rotating the token means a new worker cannot authenticate to the signer while
// the existing ones stop being issued certificates, which presents as a broken
// signer rather than as a changed secret. `kube.Ensure` is deliberately not
// used here: it would rewrite them on the first pass after any drift, and
// there is no drift that is worth that.
func (r *ManagedTenantReconciler) ensureTalosMachineSecrets(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) error {
	name := obj.Name + "-talos-secrets"
	live := &corev1.Secret{}
	err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, live)
	if err == nil {
		return nil
	}
	if !apierrors.IsNotFound(err) {
		return fmt.Errorf("reading %s/%s: %w", namespace, name, err)
	}

	token, err := bootstrapToken()
	if err != nil {
		return err
	}
	clusterID, err := randomBase64(32)
	if err != nil {
		return err
	}
	clusterSecret, err := randomBase64(32)
	if err != nil {
		return err
	}

	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: namespace,
			Labels: map[string]string{
				"kubevirt-ui.io/managed": "true",
				"kubevirt-ui.io/tenant":  obj.Name,
			},
		},
		Type: corev1.SecretTypeOpaque,
		StringData: map[string]string{
			// Also the trustd token — in Talos the two are one value.
			"machine.token":  token,
			"cluster.id":     clusterID,
			"cluster.secret": clusterSecret,
		},
	}
	if err := r.Create(ctx, secret); err != nil && !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("creating %s/%s: %w", namespace, name, err)
	}
	kube.CountWrite(r.Scheme, secret, tenantControllerName, "created")
	return nil
}

const tokenAlphabet = "abcdefghijklmnopqrstuvwxyz0123456789"

// bootstrapToken is a kubeadm-format token: six characters, a dot, sixteen.
func bootstrapToken() (string, error) {
	id, err := tokenHalf(6)
	if err != nil {
		return "", err
	}
	secret, err := tokenHalf(16)
	if err != nil {
		return "", err
	}
	return id + "." + secret, nil
}

func tokenHalf(length int) (string, error) {
	out := make([]byte, length)
	for i := range out {
		n, err := rand.Int(rand.Reader, big.NewInt(int64(len(tokenAlphabet))))
		if err != nil {
			return "", fmt.Errorf("generating a token: %w", err)
		}
		out[i] = tokenAlphabet[n.Int64()]
	}
	return string(out), nil
}

func randomBase64(size int) (string, error) {
	buffer := make([]byte, size)
	if _, err := rand.Read(buffer); err != nil {
		return "", fmt.Errorf("generating a secret: %w", err)
	}
	return base64.StdEncoding.EncodeToString(buffer), nil
}

func controlPlaneCondition(ready bool, reason, message string) metav1.Condition {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	if reason == "" {
		reason = "Provisioning"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionControlPlaneReady,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}
