package controller

import (
	"fmt"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// servingEndpoints is the endpoints controller's part, which envtest has nobody
// to play.
func servingEndpoints(t *testing.T, service string, ready bool) {
	t.Helper()
	slice := &discoveryv1.EndpointSlice{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "kubevirt-ui-system",
			Name:      service + "-abcde",
			Labels:    map[string]string{discoveryv1.LabelServiceName: service},
		},
		AddressType: discoveryv1.AddressTypeIPv4,
		Endpoints: []discoveryv1.Endpoint{{
			Addresses:  []string{"10.42.0.7"},
			Conditions: discoveryv1.EndpointConditions{Ready: ptr.To(ready)},
		}},
	}
	if err := k8sClient.Create(testCtx, slice); err != nil {
		if !apierrors.IsAlreadyExists(err) {
			t.Fatalf("creating endpoints: %v", err)
		}
		live := &discoveryv1.EndpointSlice{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kubevirt-ui-system", Name: service + "-abcde",
		}, live); err != nil {
			t.Fatalf("reading endpoints: %v", err)
		}
		live.Endpoints[0].Conditions.Ready = ptr.To(ready)
		if err := k8sClient.Update(testCtx, live); err != nil {
			t.Fatalf("updating endpoints: %v", err)
		}
	}
}

// TestAnAddressThatServesNothingIsNotReady.
//
// The failure this guards against did happen, and cost a full round of
// diagnosis pointed at the wrong object: the Service was created in the
// tenant's namespace, where a Service selects only pods beside it, so it had an
// address and no endpoints. Every query timed out, which looks exactly like a
// server refusing to answer.
func TestAnAddressThatServesNothingIsNotReady(t *testing.T) {
	mustTenant(t, talosTenant("tntp"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("tntp")
		return err
	})
	assignAddress(t, "tntp", "10.199.0.104")

	eventually(t, "the time Service", func() error {
		service := &corev1.Service{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kubevirt-ui-system", Name: "tntp-ntp",
		}, service); err != nil {
			return err
		}
		if got := service.Annotations["metallb.universe.tf/allow-shared-ip"]; got != "tntp-cp" {
			return fmt.Errorf("sharing key = %q — MetalLB refuses to share an "+
				"address unless every Service on it declares the same key", got)
		}
		if got := service.Annotations["metallb.universe.tf/loadBalancerIPs"]; got != "10.199.0.104" {
			return fmt.Errorf("asked for %q", got)
		}
		if len(service.Spec.Ports) != 1 || service.Spec.Ports[0].Protocol != corev1.ProtocolUDP {
			return fmt.Errorf("ports = %+v", service.Spec.Ports)
		}
		if service.Spec.ExternalTrafficPolicy != corev1.ServiceExternalTrafficPolicyCluster {
			return fmt.Errorf("traffic policy = %q — Local black-holes the "+
				"request from any node with no chrony replica, including the "+
				"one making it during a join", service.Spec.ExternalTrafficPolicy)
		}
		return nil
	})

	eventually(t, "the tenant to say the address serves nothing", func() error {
		condition := tenantCondition(getTenant(t, "tntp"),
			platformv1alpha1.ConditionTimeServed)
		if condition == nil || condition.Reason != "NoEndpoints" {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})

	// An endpoint that is not ready is not an answer either.
	servingEndpoints(t, "tntp-ntp", false)
	consistently(t, "an unready endpoint to count for nothing", 4*time.Second, func() error {
		condition := tenantCondition(getTenant(t, "tntp"),
			platformv1alpha1.ConditionTimeServed)
		if condition == nil || condition.Status != metav1.ConditionFalse {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})

	servingEndpoints(t, "tntp-ntp", true)

	// MetalLB's part.
	service := &corev1.Service{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kubevirt-ui-system", Name: "tntp-ntp",
	}, service); err != nil {
		t.Fatalf("reading the time Service: %v", err)
	}
	service.Status.LoadBalancer.Ingress = []corev1.LoadBalancerIngress{{IP: "10.199.0.104"}}
	if err := k8sClient.Status().Update(testCtx, service); err != nil {
		t.Fatalf("assigning the shared address: %v", err)
	}

	eventually(t, "the time to be served", func() error {
		condition := tenantCondition(getTenant(t, "tntp"),
			platformv1alpha1.ConditionTimeServed)
		if condition == nil || condition.Status != metav1.ConditionTrue {
			return fmt.Errorf("condition = %+v", condition)
		}
		return nil
	})
}

// TestAnUnsharedAddressIsReportedRatherThanIgnored. MetalLB leaves the second
// Service pending forever when the key does not match, and a pending address
// presents as a worker that cannot get the time.
func TestAnUnsharedAddressIsReportedRatherThanIgnored(t *testing.T) {
	mustTenant(t, talosTenant("tnts"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("tnts")
		return err
	})
	assignAddress(t, "tnts", "10.199.0.105")
	eventually(t, "the time Service", func() error {
		service := &corev1.Service{}
		return k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kubevirt-ui-system", Name: "tnts-ntp",
		}, service)
	})
	servingEndpoints(t, "tnts-ntp", true)

	eventually(t, "the tenant to say the address never arrived", func() error {
		condition := tenantCondition(getTenant(t, "tnts"),
			platformv1alpha1.ConditionTimeServed)
		if condition == nil {
			return fmt.Errorf("no condition")
		}
		if condition.Reason != "AddressNotShared" {
			return fmt.Errorf("reason = %q (%s)", condition.Reason, condition.Message)
		}
		if !strings.Contains(condition.Message, "10.199.0.105") {
			return fmt.Errorf("the message does not name the address: %s", condition.Message)
		}
		return nil
	})
}

// TestTheTimeServerIsOneForTheWholeCluster.
func TestTheTimeServerIsOneForTheWholeCluster(t *testing.T) {
	eventually(t, "chrony", func() error {
		deployment := &appsv1.Deployment{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kubevirt-ui-system", Name: "kubevirt-ui-ntp",
		}, deployment); err != nil {
			return err
		}
		if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas != 2 {
			return fmt.Errorf("replicas = %v", deployment.Spec.Replicas)
		}
		if len(deployment.Spec.Template.Spec.TopologySpreadConstraints) != 1 {
			return fmt.Errorf("both replicas may land on one node, putting " +
				"every future join behind a single drain")
		}
		container := deployment.Spec.Template.Spec.Containers[0]
		command := strings.Join(container.Command, " ")
		// Both scars, asserted by meaning: the stale pid file that makes one
		// crash permanent, and -x, without which chronyd tries to set the
		// node's clock and dies for lack of CAP_SYS_TIME.
		if !strings.Contains(command, "rm -f /run/chrony/chronyd.pid") {
			return fmt.Errorf("command = %q", command)
		}
		if !strings.Contains(command, "-x") {
			return fmt.Errorf("command = %q", command)
		}
		for _, capability := range container.SecurityContext.Capabilities.Add {
			if capability == "SYS_TIME" {
				return fmt.Errorf("it may set the node's clock, which is the " +
					"opposite of what it is for")
			}
		}
		return nil
	})

	config := &corev1.ConfigMap{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kubevirt-ui-system", Name: "kubevirt-ui-ntp",
	}, config); err != nil {
		t.Fatalf("reading the chrony configuration: %v", err)
	}
	// Without this chronyd answers nothing at all until it thinks it is
	// synchronised, and the pod reports Ready throughout.
	if !strings.Contains(config.Data["chrony.conf"], "local stratum 10") {
		t.Errorf("chrony.conf = %q", config.Data["chrony.conf"])
	}
}
