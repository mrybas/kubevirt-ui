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
)

// TestAVpcDnsNobodyServesIsNotCalledPending.
//
// UAT run 4, N-1: three VPCs, a VpcDns written for each, no Deployment for any
// of them, and kube-ovn's controller logging "failed to add or update vpc-dns,
// not enabled, requeuing" every time. The feature is configured by the
// ConfigMap `vpc-dns-config`, which this installation does not have.
//
// The condition said the Deployment "does not exist yet; the route goes on as
// soon as kube-ovn creates it". It never would. Meanwhile the VMs in those
// networks had no resolver at all — `curl 1.1.1.1` works, any name does not —
// and the product had declared a dependency the platform does not provide and
// then described its absence as a matter of time.
//
// The rule this is an instance of: name the missing precondition, do not
// promise the clock.
func TestAVpcDnsNobodyServesIsNotCalledPending(t *testing.T) {
	mustSharedNamespace(t, "kube-ovn")
	mustOverlaySubnet(t)

	// The feature off: no vpc-dns-config anywhere.
	config := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Namespace: "kube-ovn", Name: vpcDNSConfigMap,
	}}
	if err := k8sClient.Delete(testCtx, config); err != nil && !apierrors.IsNotFound(err) {
		t.Fatalf("clearing the vpc-dns config: %v", err)
	}
	t.Cleanup(func() { mustKubeOVNResolver(t) })

	mustNetwork(t, &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "netnodns"},
		Spec: platformv1alpha1.ManagedNetworkSpec{
			CIDR:        "10.200.132.0/22",
			ServiceCIDR: "10.96.0.0/12",
		},
	})

	eventually(t, "the missing precondition to be named", func() error {
		cond := networkCondition(
			getNetwork(t, "netnodns"), platformv1alpha1.ConditionDNSReady)
		if cond == nil || cond.Status != metav1.ConditionFalse {
			return fmt.Errorf("condition = %v", cond)
		}
		if cond.Reason != "KubeOVNVpcDNSDisabled" {
			return fmt.Errorf("reason = %q", cond.Reason)
		}
		// Which object is missing, so the diagnosis costs one kubectl.
		if !strings.Contains(cond.Message, vpcDNSConfigMap) {
			return fmt.Errorf("message does not name the ConfigMap: %q", cond.Message)
		}
		// And no promise of time.
		for _, weasel := range []string{"yet", "as soon as"} {
			if strings.Contains(cond.Message, weasel) {
				return fmt.Errorf("message promises time (%q): %q", weasel, cond.Message)
			}
		}
		return nil
	})

	// Nor is an object handed to a controller that will not take it.
	dns := &unstructured.Unstructured{}
	dns.SetGroupVersionKind(vpcDNSGVK)
	err := k8sClient.Get(testCtx, types.NamespacedName{Name: "netnodns-dns"}, dns)
	if err == nil {
		t.Fatal("a VpcDns was written for a cluster with the feature off")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the VpcDns: %v", err)
	}
}
