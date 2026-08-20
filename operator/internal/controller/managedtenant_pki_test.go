package controller

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

func certManagerObject(kind, namespace, name string) (*unstructured.Unstructured, error) {
	obj := &unstructured.Unstructured{}
	switch kind {
	case "Issuer":
		obj.SetGroupVersionKind(issuerGVK)
	default:
		obj.SetGroupVersionKind(certificateGVK)
	}
	err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: namespace, Name: name,
	}, obj)
	return obj, err
}

// TestTheSignerChainIsWrittenOnceTheAddressExists.
//
// A Talos worker asks trustd for a certificate rather than presenting a token,
// so this chain is the difference between a node that joins and a VM that looks
// healthy while the cluster has never heard of it.
func TestTheSignerChainIsWrittenOnceTheAddressExists(t *testing.T) {
	mustTenant(t, talosTenant("tpki"))

	// Before the address there is deliberately nothing: the certificate carries
	// the address as an IP SAN and is read once, at the signer's startup.
	eventually(t, "the tenant to say what it is waiting for", func() error {
		condition := tenantCondition(getTenant(t, "tpki"),
			platformv1alpha1.ConditionPKIReady)
		if condition == nil {
			return fmt.Errorf("no condition yet")
		}
		if condition.Reason != "WaitingForAddress" {
			return fmt.Errorf("reason = %q (%s)", condition.Reason, condition.Message)
		}
		return nil
	})
	if _, err := certManagerObject("Certificate", "tenant-tpki", "tpki-talos-signer"); err == nil {
		t.Fatal("a certificate was issued before there was an address to put in it")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the signer certificate: %v", err)
	}

	assignAddress(t, "tpki", "10.199.0.102")

	eventually(t, "the whole chain", func() error {
		for kind, name := range map[string]string{
			"Issuer":      "tpki-talos-selfsigned",
			"Certificate": "tpki-talos-ca",
		} {
			if _, err := certManagerObject(kind, "tenant-tpki", name); err != nil {
				return err
			}
		}
		if _, err := certManagerObject("Issuer", "tenant-tpki", "tpki-talos-ca-issuer"); err != nil {
			return err
		}
		_, err := certManagerObject("Certificate", "tenant-tpki", "tpki-talos-signer")
		return err
	})

	signer, err := certManagerObject("Certificate", "tenant-tpki", "tpki-talos-signer")
	if err != nil {
		t.Fatalf("reading the signer certificate: %v", err)
	}
	addresses, _, _ := unstructured.NestedStringSlice(signer.Object, "spec", "ipAddresses")
	if len(addresses) != 1 || addresses[0] != "10.199.0.102" {
		t.Errorf("ipAddresses = %v — a worker dials the address and sends no "+
			"SNI, so a DNS-only certificate fails the handshake", addresses)
	}
	names, _, _ := unstructured.NestedStringSlice(signer.Object, "spec", "dnsNames")
	want := []string{
		"tpki-talos.tenant-tpki.svc",
		"tpki-talos.tenant-tpki.svc.cluster.local",
	}
	if len(names) != 2 || names[0] != want[0] || names[1] != want[1] {
		t.Errorf("dnsNames = %v, want %v — the short form is what a worker puts "+
			"in the SNI and the long one is what in-cluster clients resolve",
			names, want)
	}

	// The CA is the tenant's identity anchor: rotating it means re-provisioning
	// every node, so it is issued for ten years rather than the signer's one.
	ca, err := certManagerObject("Certificate", "tenant-tpki", "tpki-talos-ca")
	if err != nil {
		t.Fatalf("reading the CA: %v", err)
	}
	if duration, _, _ := unstructured.NestedString(ca.Object, "spec", "duration"); duration != "87600h" {
		t.Errorf("CA duration = %q", duration)
	}
	if isCA, _, _ := unstructured.NestedBool(ca.Object, "spec", "isCA"); !isCA {
		t.Error("the CA is not marked as one, so it can sign nothing")
	}
	if algorithm, _, _ := unstructured.NestedString(ca.Object, "spec", "privateKey", "algorithm"); algorithm != "Ed25519" {
		t.Errorf("CA key algorithm = %q", algorithm)
	}

	// Nothing has issued it — cert-manager is not running here — so the tenant
	// says it is waiting rather than claiming a certificate exists.
	condition := tenantCondition(getTenant(t, "tpki"), platformv1alpha1.ConditionPKIReady)
	if condition == nil || condition.Status != metav1.ConditionFalse ||
		condition.Reason != "Issuing" {
		t.Errorf("condition = %+v", condition)
	}

	// And the in-cluster Service carries both ports on one host, because Talos
	// derives trustd's address from the apiserver's.
	service := &corev1.Service{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tpki", Name: "tpki-talos",
	}, service); err != nil {
		t.Fatalf("reading the Talos Service: %v", err)
	}
	ports := map[string]int32{}
	for _, port := range service.Spec.Ports {
		ports[port.Name] = port.Port
	}
	if ports["api"] != 6443 || ports["trustd"] != 50001 {
		t.Errorf("ports = %v", ports)
	}
}

// signerPEM is a certificate carrying exactly the SANs asked for — cert-manager
// is not running here, so the test plays its part.
func signerPEM(t *testing.T, ips []string, dnsNames []string) []byte {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("key: %v", err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "talos-signer"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		DNSNames:     dnsNames,
	}
	for _, ip := range ips {
		template.IPAddresses = append(template.IPAddresses, net.ParseIP(ip))
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, public, private)
	if err != nil {
		t.Fatalf("certificate: %v", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func putSignerSecret(t *testing.T, namespace, name string, pemBytes []byte) {
	t.Helper()
	secret := &corev1.Secret{}
	err := k8sClient.Get(testCtx, types.NamespacedName{Namespace: namespace, Name: name}, secret)
	switch {
	case apierrors.IsNotFound(err):
		secret = &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace, Name: name,
		}}
		secret.Data = map[string][]byte{"tls.crt": pemBytes, "tls.key": []byte("k")}
		if err := k8sClient.Create(testCtx, secret); err != nil {
			t.Fatalf("issuing the certificate: %v", err)
		}
	case err != nil:
		t.Fatalf("reading the secret: %v", err)
	default:
		secret.Data["tls.crt"] = pemBytes
		if err := k8sClient.Update(testCtx, secret); err != nil {
			t.Fatalf("re-issuing the certificate: %v", err)
		}
	}
}

// TestASignerCertificateWithoutTheAddressIsNotReady.
//
// "The secret exists" is not "the certificate answers for this address". Two
// ways that matters, both reporting a working signer while `<vip>:50001` fails
// the handshake: a certificate issued before the tenant had an address carries
// DNS names only, and adding ipAddresses to the Certificate leaves the old
// secret in place until cert-manager re-issues. A signer started in that window
// reads its certificate once and never sees the replacement.
func TestASignerCertificateWithoutTheAddressIsNotReady(t *testing.T) {
	mustTenant(t, talosTenant("tpks"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("tpks")
		return err
	})
	assignAddress(t, "tpks", "10.199.0.103")
	eventually(t, "the chain", func() error {
		_, err := certManagerObject("Certificate", "tenant-tpks", "tpks-talos-signer")
		return err
	})

	names := []string{
		"tpks-talos.tenant-tpks.svc",
		"tpks-talos.tenant-tpks.svc.cluster.local",
	}

	// The legacy shape: names only, no address.
	putSignerSecret(t, "tenant-tpks", "tpks-talos-signer", signerPEM(t, nil, names))
	eventually(t, "the tenant to refuse the DNS-only certificate", func() error {
		condition := tenantCondition(getTenant(t, "tpks"),
			platformv1alpha1.ConditionPKIReady)
		if condition == nil {
			return fmt.Errorf("no condition")
		}
		if condition.Status != metav1.ConditionFalse || condition.Reason != "Reissuing" {
			return fmt.Errorf("condition = %+v", condition)
		}
		if !strings.Contains(condition.Message, "10.199.0.103") {
			return fmt.Errorf("the message does not name the address: %s", condition.Message)
		}
		return nil
	})

	// A certificate for the wrong address is no better than none.
	putSignerSecret(t, "tenant-tpks", "tpks-talos-signer",
		signerPEM(t, []string{"10.199.0.199"}, names))
	consistently(t, "a foreign address to stay refused", 4*time.Second, func() error {
		condition := tenantCondition(getTenant(t, "tpks"),
			platformv1alpha1.ConditionPKIReady)
		if condition == nil || condition.Status != metav1.ConditionFalse {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})

	// Missing one of the two names is a handshake failure for whoever presents
	// it, so it is not ready either.
	putSignerSecret(t, "tenant-tpks", "tpks-talos-signer",
		signerPEM(t, []string{"10.199.0.103"}, names[:1]))
	consistently(t, "a half-named certificate to stay refused", 4*time.Second, func() error {
		condition := tenantCondition(getTenant(t, "tpks"),
			platformv1alpha1.ConditionPKIReady)
		if condition == nil || condition.Status != metav1.ConditionFalse {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})

	// And the real thing is accepted, so the refusals above are the check
	// working rather than a condition that never turns true.
	putSignerSecret(t, "tenant-tpks", "tpks-talos-signer",
		signerPEM(t, []string{"10.199.0.103"}, names))
	eventually(t, "the reissued certificate to be accepted", func() error {
		condition := tenantCondition(getTenant(t, "tpks"),
			platformv1alpha1.ConditionPKIReady)
		if condition == nil || condition.Status != metav1.ConditionTrue {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})
}

// TestACloudInitTenantGetsNoSignerChain. It joins with a token; there is
// nothing to sign, and a PKI condition on it could only ever be false.
func TestACloudInitTenantGetsNoSignerChain(t *testing.T) {
	mustTenant(t, plainTenant("tpkc"))

	eventually(t, "the tenant to settle", func() error {
		if tenantCondition(getTenant(t, "tpkc"),
			platformv1alpha1.ConditionNamespaceReady) == nil {
			return fmt.Errorf("not reconciled yet")
		}
		return nil
	})
	if got := tenantCondition(getTenant(t, "tpkc"),
		platformv1alpha1.ConditionPKIReady); got != nil {
		t.Errorf("a cloud-init tenant reports %+v about a signer it has not got", got)
	}
	if _, err := certManagerObject("Issuer", "tenant-tpkc", "tpkc-talos-selfsigned"); err == nil {
		t.Error("a signer chain was written for a tenant that signs nothing")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the issuer: %v", err)
	}
}
