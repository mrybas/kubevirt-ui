package controller

import (
	"fmt"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kubevirt"
	"github.com/mrybas/kubevirt-ui/operator/internal/network"
)

/*
A resolver address nobody answers on does not survive in the network.

UAT run 4, E2, second pass. The product used to invent the address of a VPC
resolver — service CIDR, 200 in the last octet — and hand it to every subnet.
That is fixed at the source, so a new network gets no DNS option at all. The
networks made before it still order the address in their own spec, and the
operator renders what is ordered: reconciling could not undo it, and there was
no migration.

The grounds for withdrawing it are a datapath fact rather than a memory of our
own mistake: an address inside the cluster's service network, on a VPC whose
vpc-dns is not enabled, is a ClusterIP with no route from that VPC. Nothing
can answer on it. An address anywhere else — a resolver on the VLAN, a public
one — is somebody's deliberate setting and is left alone.
*/

// TestTheGroundUnderAVpcDnsIsBuilt.
//
// The step that did not survive the handover, and the whole of E2. kube-ovn's
// vpc-dns controller does nothing until the attachment and two ConfigMaps
// exist, and all three are this product's to create: the backend's VPC-create
// path makes them, the operator's path made the VpcDns object and nothing
// under it. A VPC created through the operator therefore handed its guests a
// resolver address with nothing answering on it.
//
// Twice misread before it was measured — first as "this kube-ovn does not have
// the feature" (it does; the resource is `vpc-dnses`, and `kubectl get vpcdns`
// finds nothing), then as "the product invented an address" (it picks it, by a
// fixed convention, and is supposed to write it into the configuration below).
func TestTheGroundUnderAVpcDnsIsBuilt(t *testing.T) {
	mustSharedNamespace(t, "kube-ovn")
	mustOverlaySubnet(t)
	clusterDNS := mustClusterDNSService(t)

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netprereq"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:        "10.200.148.0/22",
			ServiceCIDR: "10.96.0.0/12",
		},
	})

	eventually(t, "the gate configuration to exist, with an address in it", func() error {
		cm := &corev1.ConfigMap{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kube-ovn", Name: network.VPCDNSConfigMap,
		}, cm); err != nil {
			return err
		}
		// The one value kube-ovn spins on when it is missing.
		if cm.Data["coredns-vip"] != "10.96.0.200" {
			return fmt.Errorf("coredns-vip = %q", cm.Data["coredns-vip"])
		}
		if cm.Data["enable-vpc-dns"] != "true" {
			return fmt.Errorf("the feature is not switched on: %v", cm.Data)
		}
		// The route the pod gets on its second NIC has to be the DNS it
		// forwards to, not the API server, or every query is a timeout.
		if cm.Data["k8s-service-host"] != clusterDNS {
			return fmt.Errorf("k8s-service-host = %q", cm.Data["k8s-service-host"])
		}
		return nil
	})

	eventually(t, "the Corefile to forward where that route leads", func() error {
		cm := &corev1.ConfigMap{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kube-ovn", Name: network.VPCDNSCorefileConfigMap,
		}, cm); err != nil {
			return err
		}
		if !strings.Contains(cm.Data["Corefile"], "forward . "+clusterDNS) {
			return fmt.Errorf("Corefile = %q", cm.Data["Corefile"])
		}
		return nil
	})

	eventually(t, "the attachment every VpcDns pod needs", func() error {
		nad := &unstructured.Unstructured{}
		nad.SetGroupVersionKind(nadGVK)
		return k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "default", Name: network.VPCDNSNADName,
		}, nad)
	})
}

// TestAnEmptyVIPIsNeverWritten.
//
// `enable-vpc-dns: true` with no address describes neither an enabled feature
// nor a disabled one. kube-ovn answers every VpcDns in the cluster with
// "corednsVip should be set" and builds nothing, and every check that reads
// the file's existence as an answer is told the wrong thing — including the
// one that decides whether to hand the address to a guest.
//
// It happened because the value was read out of the file it is written to: a
// circle that was self-consistent while an arithmetic supplied the address,
// and wrote the product's own ignorance into its own source of truth the
// moment that arithmetic was removed.
func TestAnEmptyVIPIsNeverWritten(t *testing.T) {
	r := &ManagedNetworkReconciler{Client: k8sClient, Scheme: k8sClient.Scheme()}
	if err := r.ensureVpcDNSPrereqs(testCtx, "kube-ovn", "", "10.0.0.10"); err != nil {
		t.Fatalf("refusing to write should not be an error: %v", err)
	}
	// Nothing was created for it, and nothing existing was blanked.
	cm := &corev1.ConfigMap{}
	err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kube-ovn", Name: network.VPCDNSConfigMap,
	}, cm)
	if err == nil && cm.Data["coredns-vip"] == "" {
		t.Fatal("a configuration with an empty coredns-vip was written")
	}
}

func TestAMachineOnAVPCSaysWhetherItCanResolve(t *testing.T) {
	for _, tc := range []struct {
		name    string
		in      func() (bool, string)
		status  metav1.ConditionStatus
		reason  string
		mustSay string
	}{
		{"on a VPC with a resolver", func() (bool, string) { return true, "10.96.0.200" },
			metav1.ConditionTrue, "VPCResolver", "10.96.0.200"},
		{"on a VPC with none", func() (bool, string) { return true, "" },
			metav1.ConditionFalse, "NoResolverInVPC", "no route from here"},
		{"not on a VPC at all", func() (bool, string) { return false, "" },
			metav1.ConditionTrue, "ClusterDNS", "cluster resolver"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			onVPC, vip := tc.in()
			in := kubevirtInputForTest(onVPC, vip)
			cond := resolvableCondition(
				&platformv1alpha1.ManagedVM{ObjectMeta: metav1.ObjectMeta{Name: "m"}}, in)
			if cond.Status != tc.status || cond.Reason != tc.reason {
				t.Fatalf("condition = %+v", cond)
			}
			if !strings.Contains(cond.Message, tc.mustSay) {
				t.Errorf("message does not say %q: %s", tc.mustSay, cond.Message)
			}
		})
	}
}

// kubevirtInputForTest is the two facts the condition reads, and nothing else.
func kubevirtInputForTest(onVPC bool, vip string) kubevirt.Input {
	in := kubevirt.Input{VPCDNSVIP: vip}
	in.Networks = []kubevirt.ResolvedNetwork{{Subnet: "s", IsVPCOverlay: onVPC}}
	return in
}

// mustClusterDNSService stands in for the cluster's own CoreDNS, which is what
// a VpcDns forwards everything to. Its address is whatever this API server
// allocates — the test asserts the forward matches it rather than a literal,
// because the point is that the two agree.
func mustClusterDNSService(t *testing.T) string {
	t.Helper()
	mustSharedNamespace(t, "kube-system")
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Namespace: "kube-system", Name: "kube-dns"},
		Spec: corev1.ServiceSpec{
			Ports: []corev1.ServicePort{{Name: "dns", Port: 53, Protocol: corev1.ProtocolUDP}},
		},
	}
	if err := k8sClient.Create(testCtx, svc); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting the cluster resolver: %v", err)
	}
	live := &corev1.Service{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kube-system", Name: "kube-dns",
	}, live); err != nil {
		t.Fatalf("reading the cluster resolver: %v", err)
	}
	return live.Spec.ClusterIP
}
