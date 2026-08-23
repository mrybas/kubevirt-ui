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

func TestAnUnreachableResolverIsNotProgrammed(t *testing.T) {
	mustSharedNamespace(t, "kube-ovn")
	mustOverlaySubnet(t)

	// The feature off, as on the stand.
	config := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Namespace: "kube-ovn", Name: vpcDNSConfigMap,
	}}
	if err := k8sClient.Delete(testCtx, config); err != nil && !apierrors.IsNotFound(err) {
		t.Fatalf("clearing the vpc-dns config: %v", err)
	}
	t.Cleanup(func() { mustKubeOVNResolver(t) })

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netdeadns"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:        "10.200.140.0/22",
			ServiceCIDR: "10.96.0.0/12",
			// What every network created before the fix carries.
			DNSServer: "10.96.0.200",
		},
	})

	// The subnet does not carry it. That is the half a guest can feel.
	eventually(t, "the dead address to stay out of the datapath", func() error {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netdeadns-default",
		}, subnet); err != nil {
			return err
		}
		opts, _, _ := unstructured.NestedString(subnet.Object, "spec", "dhcpV4Options")
		if strings.Contains(opts, "dns_server") {
			return fmt.Errorf("the subnet still hands it out: %q", opts)
		}
		return nil
	})

	// And the condition names it, so the declaration can be corrected by
	// whoever made it.
	eventually(t, "the network to say what it refused and why", func() error {
		cond := networkCondition(
			getNetwork(t, "netdeadns"), platformv1alpha1.ConditionDNSReady)
		if cond == nil || cond.Reason != "KubeOVNVpcDNSDisabled" {
			return fmt.Errorf("condition = %v", cond)
		}
		for _, want := range []string{"10.96.0.200", "no route", "spec.dnsServer"} {
			if !strings.Contains(cond.Message, want) {
				return fmt.Errorf("message does not say %q: %s", want, cond.Message)
			}
		}
		return nil
	})

	// The declaration itself is untouched: a controller that edits the spec
	// it was given is a second writer of somebody else's field.
	got := getNetwork(t, "netdeadns")
	if got.Spec.DNSServer != "10.96.0.200" {
		t.Fatalf("the operator rewrote the spec: dnsServer = %q", got.Spec.DNSServer)
	}
}

func TestAResolverSomewhereElseIsLeftAlone(t *testing.T) {
	mustSharedNamespace(t, "kube-ovn")
	mustOverlaySubnet(t)

	config := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Namespace: "kube-ovn", Name: vpcDNSConfigMap,
	}}
	if err := k8sClient.Delete(testCtx, config); err != nil && !apierrors.IsNotFound(err) {
		t.Fatalf("clearing the vpc-dns config: %v", err)
	}
	t.Cleanup(func() { mustKubeOVNResolver(t) })

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netownns"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:        "10.200.144.0/22",
			ServiceCIDR: "10.96.0.0/12",
			// A resolver an admin chose, outside the service network: on the
			// VLAN, or public. Nothing here knows whether it answers, and
			// nothing here is entitled to decide it does not.
			DNSServer: "10.199.4.53",
		},
	})

	// Give the controller the same chance to touch it, then check it did not.
	eventually(t, "the network to be reconciled at all", func() error {
		cond := networkCondition(
			getNetwork(t, "netownns"), platformv1alpha1.ConditionDNSReady)
		if cond == nil {
			return fmt.Errorf("not reconciled yet")
		}
		return nil
	})
	got := getNetwork(t, "netownns")
	if got.Spec.DNSServer != "10.199.4.53" {
		t.Fatalf("somebody else's resolver was withdrawn: %q", got.Spec.DNSServer)
	}

	// And it is programmed, because nothing here knows it does not answer.
	eventually(t, "the subnet to hand it out", func() error {
		subnet := &unstructured.Unstructured{}
		subnet.SetGroupVersionKind(subnetGVK)
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Name: "netownns-default",
		}, subnet); err != nil {
			return err
		}
		opts, _, _ := unstructured.NestedString(subnet.Object, "spec", "dhcpV4Options")
		if !strings.Contains(opts, "dns_server=10.199.4.53") {
			return fmt.Errorf("dhcpV4Options = %q", opts)
		}
		return nil
	})
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
