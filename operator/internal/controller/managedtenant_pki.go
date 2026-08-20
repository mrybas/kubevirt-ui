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
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"net/netip"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

const (
	certManagerGroup   = "cert-manager.io"
	certManagerVersion = "v1"
)

var (
	issuerGVK = schema.GroupVersionKind{
		Group: certManagerGroup, Version: certManagerVersion, Kind: "Issuer",
	}
	certificateGVK = schema.GroupVersionKind{
		Group: certManagerGroup, Version: certManagerVersion, Kind: "Certificate",
	}
)

// talosServiceName is the Service Talos treats as the control plane.
//
// Not Kamaji's own: Talos dials trustd on port 50001 of whatever host is in
// `cluster.controlPlane.endpoint`, and Kamaji cannot put a second port on the
// Service it manages — a port added by hand is reconciled away in under a
// minute. A Service of our own avoids all of it: same selector, both ports.
func talosServiceName(name string) string {
	return name + "-talos"
}

// signerDNSNames are the names the signer certificate must satisfy.
//
// Both forms: a worker dialling the short name puts it in the TLS SNI,
// in-cluster clients resolve the long one, and the certificate has to answer
// for whichever is presented.
func signerDNSNames(name, namespace string) []string {
	service := talosServiceName(name)
	return []string{
		service + "." + namespace + ".svc",
		service + "." + namespace + ".svc.cluster.local",
	}
}

// reconcilePKI writes the cert-manager chain the CSR signer runs on:
// selfSigned issuer, a CA, an issuer from that CA, and the signer's own
// certificate.
//
// **It waits for the address.** The rule used to be DNS names only, for a good
// reason — an IP SAN could only be filled in after the address existed, which
// meant patching the certificate afterwards, and the signer reads its
// certificate once at startup and never watches the file. Per-tenant addresses
// remove that ordering problem, so the SAN is issued with the certificate; and
// it is required, because a worker dials `<vip>:50001` by address, sends no
// SNI, and a DNS-only certificate fails the handshake before trustd is asked
// anything at all.
func (r *ManagedTenantReconciler) reconcilePKI(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace, vip string,
) (ready bool, reason, message string, err error) {
	if obj.Spec.Workers.OS != "talos" {
		// Nothing signs CSRs for a cloud-init tenant; it joins with a token.
		return true, "", "", nil
	}
	if vip == "" {
		return false, "WaitingForAddress", "waiting for the tenant's address: " +
			"the signer's certificate needs it as an IP SAN, and a worker " +
			"dialling the address sends no SNI for a DNS name to match", nil
	}

	if err := r.ensureTalosService(ctx, obj, namespace); err != nil {
		return false, "", "", err
	}

	labels := map[string]string{
		"kubevirt-ui.io/managed": "true",
		"kubevirt-ui.io/tenant":  obj.Name,
	}
	dnsNames := make([]any, 0, 2)
	for _, name := range signerDNSNames(obj.Name, namespace) {
		dnsNames = append(dnsNames, name)
	}

	chain := []struct {
		gvk  schema.GroupVersionKind
		name string
		spec map[string]any
	}{
		{issuerGVK, obj.Name + "-talos-selfsigned", map[string]any{
			"selfSigned": map[string]any{},
		}},
		{certificateGVK, obj.Name + "-talos-ca", map[string]any{
			"isCA":       true,
			"commonName": obj.Name + "-talos-ca",
			"secretName": obj.Name + "-talos-ca",
			// Ten years: this CA is the tenant's identity anchor, and rotating
			// it means re-provisioning every node.
			"duration":    "87600h",
			"renewBefore": "720h",
			// Ed25519: small, fast, and supported by Talos's own tooling.
			"privateKey": map[string]any{"algorithm": "Ed25519"},
			"issuerRef": map[string]any{
				"name": obj.Name + "-talos-selfsigned",
				"kind": "Issuer", "group": certManagerGroup,
			},
		}},
		{issuerGVK, obj.Name + "-talos-ca-issuer", map[string]any{
			"ca": map[string]any{"secretName": obj.Name + "-talos-ca"},
		}},
		{certificateGVK, obj.Name + "-talos-signer", map[string]any{
			"secretName":  obj.Name + "-talos-signer",
			"duration":    "8760h",
			"renewBefore": "720h",
			"privateKey":  map[string]any{"algorithm": "Ed25519"},
			"dnsNames":    dnsNames,
			"ipAddresses": []any{vip},
			"issuerRef": map[string]any{
				"name": obj.Name + "-talos-ca-issuer",
				"kind": "Issuer", "group": certManagerGroup,
			},
		}},
	}

	for _, item := range chain {
		live := &unstructured.Unstructured{}
		live.SetGroupVersionKind(item.gvk)
		live.SetName(item.name)
		live.SetNamespace(namespace)
		spec := item.spec
		if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
			live.SetLabels(labels)
			return unstructured.SetNestedMap(live.Object, spec, "spec")
		}); err != nil {
			if apierrors.IsNotFound(err) || apimetaIsNoMatch(err) {
				return false, "CertManagerMissing", fmt.Sprintf(
					"Talos workers need cert-manager for the CSR signer's PKI, "+
						"and %s is not installed. Install cert-manager, or "+
						"build this tenant's workers from cloud-init.",
					item.gvk.Kind+"."+certManagerGroup), nil
			}
			return false, "", "", fmt.Errorf("writing %s %s/%s: %w",
				item.gvk.Kind, namespace, item.name, err)
		}
	}

	// Reported from the secret rather than the Certificate's own conditions:
	// the secret is what the signer container mounts, and its absence is what
	// stops the join.
	//
	// And from what is *inside* the secret, not from its existence. Two ways
	// that matters, both of which report a working signer while `<vip>:50001`
	// fails the handshake: a certificate issued before this tenant had an
	// address carries DNS names only, and adding `ipAddresses` to the
	// Certificate leaves the old secret in place until cert-manager re-issues.
	// A signer started in that window reads its certificate once and never
	// picks up the replacement.
	secret := &corev1.Secret{}
	err = r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: obj.Name + "-talos-signer",
	}, secret)
	if apierrors.IsNotFound(err) {
		return false, "Issuing", fmt.Sprintf(
			"waiting for cert-manager to issue %s/%s-talos-signer",
			namespace, obj.Name), nil
	}
	if err != nil {
		return false, "", "", fmt.Errorf("reading the signer secret: %w", err)
	}

	wanted := signerDNSNames(obj.Name, namespace)
	if missing := certificateMisses(secret.Data["tls.crt"], vip, wanted); missing != "" {
		return false, "Reissuing", fmt.Sprintf(
			"%s/%s-talos-signer exists but %s, so a worker dialling %s:%d would "+
				"fail the handshake. Waiting for cert-manager to re-issue it.",
			namespace, obj.Name, missing, vip, tenantTrustdPort), nil
	}
	return true, "Issued", fmt.Sprintf(
		"signer certificate answers for %s and %v", vip, wanted), nil
}

// certificateMisses says what a signer certificate does not cover, or "" when
// it covers everything the worker will present.
func certificateMisses(pemBytes []byte, vip string, dnsNames []string) string {
	if len(pemBytes) == 0 {
		return "carries no certificate"
	}
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return "does not contain a PEM certificate"
	}
	parsed, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return "does not parse as a certificate: " + err.Error()
	}

	address, err := netip.ParseAddr(vip)
	if err != nil {
		// Not the certificate's fault, and not something re-issuing fixes.
		return ""
	}
	covered := false
	for _, ip := range parsed.IPAddresses {
		if parsed, ok := netip.AddrFromSlice(ip); ok && parsed.Unmap() == address {
			covered = true
			break
		}
	}
	if !covered {
		return fmt.Sprintf("does not carry %s as an IP SAN (it has %v)",
			vip, parsed.IPAddresses)
	}
	for _, wanted := range dnsNames {
		found := false
		for _, name := range parsed.DNSNames {
			if name == wanted {
				found = true
				break
			}
		}
		if !found {
			return fmt.Sprintf("does not carry the name %s (it has %v)",
				wanted, parsed.DNSNames)
		}
	}
	return ""
}

// ensureTalosService publishes the apiserver and trustd on one host, because
// Talos derives the second address from the first and will not be told
// otherwise.
func (r *ManagedTenantReconciler) ensureTalosService(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) error {
	service := &corev1.Service{}
	service.Name = talosServiceName(obj.Name)
	service.Namespace = namespace
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, service, func() error {
		if service.Labels == nil {
			service.Labels = map[string]string{}
		}
		service.Labels["kubevirt-ui.io/managed"] = "true"
		service.Labels["kubevirt-ui.io/tenant"] = obj.Name
		service.Spec.Type = corev1.ServiceTypeClusterIP
		service.Spec.Selector = map[string]string{"kamaji.clastix.io/name": obj.Name}
		service.Spec.Ports = cpPorts(obj)
		return nil
	}); err != nil {
		return fmt.Errorf("publishing %s's control plane in-cluster: %w", obj.Name, err)
	}
	return nil
}

func pkiCondition(ready bool, reason, message string) metav1.Condition {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	if reason == "" {
		reason = "Issuing"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionPKIReady,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}
