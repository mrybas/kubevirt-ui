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
	"fmt"
	"os"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

const (
	ntpPort      = 123
	ntpApp       = "kubevirt-ui-ntp"
	ntpImageEnv  = "TENANTS_CHRONY_IMAGE"
	defaultNTPIm = "cturra/ntp:latest"
)

// chronyConf is byte-identical to the configuration the backend writes.
//
// Deliberately, and it was measured otherwise first: with the text shortened —
// the same directives, the explanation moved into this comment — an operator
// pass rewrote the ConfigMap the backend had just written. Harmless in itself,
// since nothing reloads chronyd on a ConfigMap change, but it is two renderers
// of one object, and the only reason it does not become a fight is that the
// backend has been retired from this write. Identical text means it would not
// be a fight even if it had not.
//
// The explanation belongs in the file rather than here for the same reason:
// whoever reads it reads it in the cluster.
const chronyConf = `# Serve the node's clock, with no upstream of our own.
#
# ` + "`" + `local stratum 10` + "`" + ` is not optional here and its absence is silent: chronyd
# refuses to answer at all until it considers itself synchronised, so without
# it the pod runs, reports Ready, and every query times out. Measured — the
# deployment was 1/1 and ` + "`" + `ntpd -q` + "`" + ` against both the Service and the pod IP
# returned nothing.
#
# The clock it serves is the node's, which the platform keeps correct; stratum
# 10 says plainly that this is a local reference and not a traceable chain.
local stratum 10

# Clients are tenant workers arriving through their own transit VIP; the
# Service and the transit ACL are what actually scope this.
allow all

bindaddress 0.0.0.0
bindcmdaddress /run/chrony/chronyd.sock
driftfile /var/lib/chrony/chrony.drift
`

func ntpNamespace() string {
	// The same namespace the shared images live in, and for the same reason:
	// what every tenant uses cannot live inside one of them.
	return goldenNamespace()
}

func chronyImage() string {
	if image := os.Getenv(ntpImageEnv); image != "" {
		return image
	}
	return defaultNTPIm
}

// reconcileTime gives the tenant somewhere to get the time before it has an
// egress.
//
// Talos will not start a kubelet against an unsynchronised clock, so a worker
// in an isolated VPC needs a time source reachable on the transit plane — and
// the tenant's own address is exactly that. One chrony for the cluster behind
// it, serving the nodes' own clock: the container's clock *is* the node's, and
// the nodes are synchronised by the platform.
func (r *ManagedTenantReconciler) reconcileTime(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, vip string,
) (ready bool, reason, message string, err error) {
	if vip == "" {
		// Either the tenant is on the default overlay, where egress and public
		// time servers are not in question, or its address has not arrived yet.
		// The caller knows which; here there is simply nowhere to publish.
		return false, "WaitingForAddress",
			"waiting for the tenant's address: the time is served on it", nil
	}

	namespace := ntpNamespace()
	if err := r.ensureChrony(ctx, namespace); err != nil {
		return false, "", "", err
	}

	service := &corev1.Service{}
	service.Name = obj.Name + "-ntp"
	// **chrony's** namespace, not the tenant's. A Service selects only pods
	// beside it, so the tenant-namespace version had no endpoints at all: it
	// existed, it had an address, and every query timed out. That looks exactly
	// like a server refusing to answer, which is why the first diagnosis of
	// this went to chronyd's configuration instead of here.
	service.Namespace = namespace
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, service, func() error {
		if service.Labels == nil {
			service.Labels = map[string]string{}
		}
		service.Labels["kubevirt-ui.io/managed"] = "true"
		service.Labels["kubevirt-ui.io/tenant"] = obj.Name
		if service.Annotations == nil {
			service.Annotations = map[string]string{}
		}
		service.Annotations["service.cilium.io/type"] = "ClusterIP"
		service.Annotations["metallb.universe.tf/loadBalancerIPs"] = vip
		// Both Services on this address must declare the key. MetalLB refuses
		// the second outright when only it does, and the address stays pending
		// forever — which presents as a worker that cannot get the time.
		service.Annotations["metallb.universe.tf/allow-shared-ip"] = cpSharingKey(obj.Name)
		service.Spec.Type = corev1.ServiceTypeLoadBalancer
		// Cluster deliberately: chrony does not run on every node, and Local
		// would black-hole the request from any node without a replica —
		// including, during a join, the node making it.
		service.Spec.ExternalTrafficPolicy = corev1.ServiceExternalTrafficPolicyCluster
		service.Spec.Selector = map[string]string{"app": ntpApp}
		service.Spec.Ports = []corev1.ServicePort{{
			Name: "ntp", Port: ntpPort, TargetPort: intstr.FromInt32(ntpPort),
			Protocol: corev1.ProtocolUDP,
		}}
		return nil
	}); err != nil {
		return false, "", "", fmt.Errorf("publishing %s's time source: %w", obj.Name, err)
	}

	// An address is not a server. The Service can exist, hold the address, and
	// select nothing — which is precisely how this failed the first time.
	slices := &discoveryv1.EndpointSliceList{}
	if err := r.List(ctx, slices, client.InNamespace(namespace),
		client.MatchingLabels{discoveryv1.LabelServiceName: service.Name}); err != nil {
		return false, "", "", fmt.Errorf("reading the time source's endpoints: %w", err)
	}
	serving := 0
	for _, slice := range slices.Items {
		for _, endpoint := range slice.Endpoints {
			if endpoint.Conditions.Ready == nil || *endpoint.Conditions.Ready {
				serving++
			}
		}
	}
	if serving == 0 {
		return false, "NoEndpoints", fmt.Sprintf(
			"%s/%s holds %s but selects no ready chrony pod, so a worker asking "+
				"for the time gets no answer at all",
			namespace, service.Name, vip), nil
	}

	assigned := ""
	for _, ingress := range service.Status.LoadBalancer.Ingress {
		if ingress.IP != "" {
			assigned = ingress.IP
			break
		}
	}
	if assigned != vip {
		return false, "AddressNotShared", fmt.Sprintf(
			"%s/%s asked for %s and has %q. MetalLB refuses to share an address "+
				"unless every Service on it carries the same sharing key.",
			namespace, service.Name, vip, assigned), nil
	}
	return true, "Served", fmt.Sprintf("%s:%d, %d chrony endpoint(s)",
		vip, ntpPort, serving), nil
}

// ensureChrony writes the one time server the whole cluster shares.
func (r *ManagedTenantReconciler) ensureChrony(ctx context.Context, namespace string) error {
	config := &corev1.ConfigMap{}
	config.Name = ntpApp
	config.Namespace = namespace
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, config, func() error {
		config.Labels = map[string]string{"app": ntpApp, "kubevirt-ui.io/managed": "true"}
		config.Data = map[string]string{"chrony.conf": chronyConf}
		return nil
	}); err != nil {
		return fmt.Errorf("writing the chrony configuration: %w", err)
	}

	want := chronyPodTemplate()
	deployment := &appsv1.Deployment{}
	deployment.Name = ntpApp
	deployment.Namespace = namespace
	if _, err := kube.Ensure(ctx, r.Client, tenantControllerName, deployment, func() error {
		deployment.Labels = map[string]string{"app": ntpApp, "kubevirt-ui.io/managed": "true"}
		// Two, spread across nodes: a tenant that cannot get the time cannot
		// get a node, so both replicas on one host would put every future join
		// behind a single drain.
		deployment.Spec.Replicas = ptr.To[int32](2)
		deployment.Spec.Selector = &metav1.LabelSelector{
			MatchLabels: map[string]string{"app": ntpApp},
		}
		mergePodTemplate(&deployment.Spec.Template, want)
		deployment.Spec.Template.Spec.TopologySpreadConstraints =
			want.Spec.TopologySpreadConstraints
		for i := range deployment.Spec.Template.Spec.Containers {
			if deployment.Spec.Template.Spec.Containers[i].Name == "chrony" {
				deployment.Spec.Template.Spec.Containers[i].Ports =
					want.Spec.Containers[0].Ports
			}
		}
		return nil
	}); err != nil {
		return fmt.Errorf("writing the chrony deployment: %w", err)
	}
	return nil
}

func chronyPodTemplate() corev1.PodTemplateSpec {
	return corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": ntpApp}},
		Spec: corev1.PodSpec{
			TopologySpreadConstraints: []corev1.TopologySpreadConstraint{{
				MaxSkew:           1,
				TopologyKey:       "kubernetes.io/hostname",
				WhenUnsatisfiable: corev1.ScheduleAnyway,
				LabelSelector: &metav1.LabelSelector{
					MatchLabels: map[string]string{"app": ntpApp},
				},
			}},
			Containers: []corev1.Container{{
				Name:  "chrony",
				Image: chronyImage(),
				Env: []corev1.EnvVar{
					{Name: "LOG_LEVEL", Value: "0"},
					{Name: "ENABLE_NTS", Value: "false"},
				},
				Ports: []corev1.ContainerPort{{
					Name: "ntp", ContainerPort: ntpPort, Protocol: corev1.ProtocolUDP,
				}},
				SecurityContext: &corev1.SecurityContext{
					AllowPrivilegeEscalation: ptr.To(false),
					Capabilities: &corev1.Capabilities{
						// Measured, not guessed. With drop ALL and
						// NET_BIND_SERVICE alone the pod crash-looped:
						//
						//   chown: /var/lib/chrony: Operation not permitted
						//   Could not open /var/run/chrony/chronyd.pid
						//
						// chronyd's entrypoint takes ownership of its state and
						// run directories and then drops privileges itself. It
						// still may not step the clock — SYS_TIME is
						// deliberately absent; this serves a clock, it does not
						// set one.
						Drop: []corev1.Capability{"ALL"},
						Add: []corev1.Capability{
							"NET_BIND_SERVICE", "CHOWN", "DAC_OVERRIDE",
							"SETUID", "SETGID",
						},
					},
				},
				// The pid file has to be cleared before start, and the reason is
				// a trap worth naming: chronyd *is* pid 1 here, so it writes "1"
				// into its pid file. `/run/chrony` is an emptyDir and survives a
				// container restart, so the next chronyd reads a stale pid of 1,
				// asks whether that process is alive — it always is, it is
				// chronyd itself — and dies with
				//
				//   Fatal error : Another chronyd may already be running (pid=1)
				//
				// forever. One crash makes the pod permanently unstartable,
				// which is how a rollout sits in CrashLoopBackOff while an older
				// ReplicaSet keeps serving the image's own configuration.
				//
				// -x is what "serves a clock, never sets one" means to chronyd.
				// Without it it tries to discipline the system clock at startup
				// and dies with `adjtimex(0x8001) failed`; the alternative is
				// handing a pod CAP_SYS_TIME, which is the opposite of intent.
				Command: []string{"sh", "-c",
					"rm -f /run/chrony/chronyd.pid; " +
						"exec chronyd -d -x -f /etc/chrony/chrony.conf"},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "run", MountPath: "/run/chrony"},
					{Name: "state", MountPath: "/var/lib/chrony"},
					{Name: "conf", MountPath: "/etc/chrony"},
				},
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("10m"),
						corev1.ResourceMemory: resource.MustParse("32Mi"),
					},
					Limits: corev1.ResourceList{
						corev1.ResourceMemory: resource.MustParse("64Mi"),
					},
				},
			}},
			// The image writes a pid file and a drift file; the root filesystem
			// gives it neither directory.
			Volumes: []corev1.Volume{
				{Name: "run", VolumeSource: corev1.VolumeSource{
					EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				{Name: "state", VolumeSource: corev1.VolumeSource{
					EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				{Name: "conf", VolumeSource: corev1.VolumeSource{
					ConfigMap: &corev1.ConfigMapVolumeSource{
						LocalObjectReference: corev1.LocalObjectReference{Name: ntpApp}}}},
			},
		},
	}
}

func timeCondition(ready bool, reason, message string) metav1.Condition {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	if reason == "" {
		reason = "Pending"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionTimeServed,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}
