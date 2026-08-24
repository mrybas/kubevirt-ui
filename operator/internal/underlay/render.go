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

// Package underlay renders the fabric a VPC egress gateway needs: a
// ProviderNetwork on a dedicated NIC, a Vlan, an external Subnet, and the NAD
// that ties them together.
//
// Kept free of a client so the shapes can be checked without a cluster. Two of
// the objects are workarounds rather than architecture and say so in their own
// labels, together with the condition that retires them.
package underlay

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

const (
	// ManagedLabel marks everything this operator builds.
	ManagedLabel = "kubevirt-ui.io/managed"
	// InfraPurposeLabel is how the rest of the UI finds the external subnet.
	InfraPurposeLabel = "kubevirt-ui.io/purpose"

	// WorkaroundLabel marks an object that exists because of somebody else's
	// bug, so it can be selected and, one day, deleted.
	WorkaroundLabel = "kubevirt-ui.io/workaround"
	// WorkaroundReason and WorkaroundRemoveWhen are prose and therefore
	// annotations. They were label values once: the API server refuses those
	// outright — no spaces, 63 characters — so both DaemonSets came back 422
	// while the four fabric objects were created. The result was an underlay
	// that looked built and had no link watcher behind it.
	WorkaroundReason     = "kubevirt-ui.io/workaround-reason"
	WorkaroundRemoveWhen = "kubevirt-ui.io/workaround-remove-when"

	// LinkWatcherName keeps provider NICs administratively up.
	LinkWatcherName = "provider-link-up"
	// CiliumExemptName clears source-IP verification on gateway endpoints.
	CiliumExemptName = "cilium-gateway-exempt"

	// ExternalGWLabel is what every gateway-bound workload selects on.
	ExternalGWLabel = "ovn.kubernetes.io/external-gw"

	// KubeOVNCNIDaemonSet is where the link watcher's default image comes from:
	// kube-ovn's CNI runs on exactly the nodes the watcher runs on, and carries
	// iproute2 because it uses it itself.
	KubeOVNCNIDaemonSet = "kube-ovn-cni"

	// FallbackWatcherImage is used only when that lookup fails. Public and
	// therefore rottable — the previous default stopped serving a layer and the
	// watcher never started.
	FallbackWatcherImage = "docker.io/library/busybox:1.36"

	kubeOVNGroup   = "kubeovn.io"
	kubeOVNVersion = "v1"
)

func managedLabels() map[string]string {
	return map[string]string{ManagedLabel: "true"}
}

// ProviderNetwork is the OVS bridge on the dedicated NIC.
func ProviderNetwork(u *platformv1alpha1.ManagedUnderlay) *unstructured.Unstructured {
	spec := map[string]any{"defaultInterface": u.Spec.Interface}
	if len(u.Spec.ExcludeNodes) > 0 {
		spec["excludeNodes"] = toAnySlice(u.Spec.ExcludeNodes)
	}
	obj := newKubeOVN("ProviderNetwork", ProviderNetworkName(u))
	obj.SetLabels(managedLabels())
	_ = unstructured.SetNestedMap(obj.Object, spec, "spec")
	return obj
}

// Vlan carries the tag, or 0 for untagged.
func Vlan(u *platformv1alpha1.ManagedUnderlay) *unstructured.Unstructured {
	obj := newKubeOVN("Vlan", VLANName(u))
	obj.SetLabels(managedLabels())
	_ = unstructured.SetNestedMap(obj.Object, map[string]any{
		"id":       int64(u.Spec.VLANID),
		"provider": ProviderNetworkName(u),
	}, "spec")
	return obj
}

// Provider is the `<nad>.<namespace>.ovn` string tying the subnet to its NAD.
//
// Structural, and compared character for character by kube-ovn: get it wrong
// and the egress gateway is refused outright with "please set correct provider
// of subnet ... to get the network-attachment-definition".
func Provider(u *platformv1alpha1.ManagedUnderlay, kubeOVNNamespace string) string {
	return fmt.Sprintf("%s.%s.ovn", SubnetName(u), kubeOVNNamespace)
}

// ExternalNAD lets kube-ovn attach a gateway's external interface through
// Multus rather than the primary CNI. Without it the gateway cannot be created
// at all.
func ExternalNAD(u *platformv1alpha1.ManagedUnderlay, kubeOVNNamespace string) *unstructured.Unstructured {
	config, _ := json.Marshal(map[string]any{
		"cniVersion":    "0.3.1",
		"type":          "kube-ovn",
		"server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
		"provider":      Provider(u, kubeOVNNamespace),
	})
	obj := &unstructured.Unstructured{Object: map[string]any{}}
	obj.SetAPIVersion("k8s.cni.cncf.io/v1")
	obj.SetKind("NetworkAttachmentDefinition")
	obj.SetName(SubnetName(u))
	obj.SetNamespace(kubeOVNNamespace)
	obj.SetLabels(managedLabels())
	_ = unstructured.SetNestedMap(obj.Object, map[string]any{
		"config": string(config),
	}, "spec")
	return obj
}

// ExternalSubnet is the address space of the segment behind the NIC.
func ExternalSubnet(u *platformv1alpha1.ManagedUnderlay, kubeOVNNamespace string) *unstructured.Unstructured {
	spec := map[string]any{
		"protocol":  "IPv4",
		"cidrBlock": u.Spec.ExternalCIDR,
		"gateway":   u.Spec.ExternalGateway,
		"vlan":      VLANName(u),
		"provider":  Provider(u, kubeOVNNamespace),
		// The gateways SNAT (or route) for themselves; the underlay must not.
		"natOutgoing": false,
		// The gateway address belongs to upstream kit that need not answer
		// kube-ovn's ping, and a failed check blocks the subnet.
		"disableGatewayCheck": true,
	}
	if len(u.Spec.ExcludeIPs) > 0 {
		spec["excludeIps"] = toAnySlice(u.Spec.ExcludeIPs)
	}
	obj := newKubeOVN("Subnet", SubnetName(u))
	obj.SetLabels(map[string]string{
		ManagedLabel:      "true",
		InfraPurposeLabel: "infrastructure",
	})
	_ = unstructured.SetNestedMap(obj.Object, spec, "spec")
	return obj
}

// LinkWatcherScript is the loop the watcher runs. `ip link set up` on an
// already-up interface is a no-op, so it costs nothing; it exists only because
// kube-ovn never rechecks the link after bridge init.
func LinkWatcherScript(interfaces []string) string {
	var b strings.Builder
	b.WriteString("while true; do\n")
	fmt.Fprintf(&b, "  for i in %s; do\n", strings.Join(interfaces, " "))
	b.WriteString("    ip link set dev \"$i\" up 2>/dev/null\n")
	b.WriteString("  done\n")
	b.WriteString("  sleep 10\n")
	b.WriteString("done\n")
	return b.String()
}

// WatchedInterfaces is every provider NIC in the cluster, this underlay's
// included and first-class.
//
// There is one link-watcher DaemonSet per cluster, so a per-underlay script
// meant the second build silently disowned the first NIC: after the egress
// underlay was added the watcher ran only `eth0.310`, `eth0.300` went down on
// two workers, and the control-plane transit went with it while OVS still
// showed the port and the subnet still read Ready. Underlays come in pairs in
// the target design, so that is the normal path, not the unlucky one.
func WatchedInterfaces(mine string, others []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, name := range append([]string{mine}, others...) {
		name = strings.TrimSpace(name)
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		out = append(out, name)
	}
	// The set is assembled from objects whose listing order the API server does
	// not promise. Sorting keeps the rendered script byte-stable, so the
	// write-on-diff check does not rewrite the DaemonSet — and restart every
	// watcher pod — on a pass where nothing changed.
	sort.Strings(out)
	return out
}

// LinkWatcher is the DaemonSet keeping every provider NIC up.
func LinkWatcher(
	u *platformv1alpha1.ManagedUnderlay, kubeOVNNamespace, image string, interfaces []string,
) *appsv1.DaemonSet {
	resolved := u.Spec.LinkWatcherImage
	if resolved == "" {
		resolved = image
	}
	if resolved == "" {
		resolved = FallbackWatcherImage
	}
	ds := workaroundDaemonSet(
		LinkWatcherName, kubeOVNNamespace,
		"kube-ovn does not re-assert the provider link",
		"kube-ovn re-raises the provider interface after bridge init, or the "+
			"node OS keeps it up on its own — then delete this DaemonSet and "+
			"confirm tx counters still move on the provider NIC "+
			"(ovs-ofctl dump-ports).",
	)
	// Only nodes that actually carry the provider NIC.
	ds.Spec.Template.Spec.NodeSelector = map[string]string{ExternalGWLabel: "true"}
	privileged := true
	ds.Spec.Template.Spec.Containers = []corev1.Container{{
		Name:            "link-up",
		Image:           resolved,
		SecurityContext: &corev1.SecurityContext{Privileged: &privileged},
		Command:         []string{"/bin/sh", "-c"},
		Args:            []string{LinkWatcherScript(interfaces)},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("5m"),
				corev1.ResourceMemory: resource.MustParse("16Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceMemory: resource.MustParse("32Mi"),
			},
		},
	}}
	return ds
}

// CiliumExemptScript walks Cilium's own endpoint list and clears source-IP
// verification on the gateway ones.
//
// Endpoint ids come first in a block and labels follow underneath, so it tracks
// the last id seen and emits it when a gateway label shows up. Selecting on the
// label kube-ovn puts on gateway workloads means VPCs created later are covered
// without anyone remembering to.
const CiliumExemptScript = `while true; do
  cilium-dbg endpoint list 2>/dev/null | awk '
    /^[0-9]+/            { id = $1 }
    /vpc-egress-gateway/ { if (id != "") { print id; id = "" } }
    /vpc-nat-gw/         { if (id != "") { print id; id = "" } }
  ' | sort -u | while read -r ep; do
    if cilium-dbg endpoint config "$ep" 2>/dev/null | grep -qi "SourceIPVerification *: *Enabled"; then
      echo "exempting endpoint $ep (VPC gateway)"
      cilium-dbg endpoint config "$ep" SourceIPVerification=disable 2>&1 | tail -1
    fi
  done
  sleep 15
done
`

// CiliumExempt is the DaemonSet exempting VPC gateway endpoints from Cilium's
// source-IP check.
//
// An egress gateway is a router forwarding replies from the whole internet,
// which Cilium in chaining mode drops as "Invalid source ip".
func CiliumExempt(u *platformv1alpha1.ManagedUnderlay, namespace string) *appsv1.DaemonSet {
	image := u.Spec.CiliumImage
	if image == "" {
		image = "quay.io/cilium/cilium:v1.20.0"
	}
	ds := workaroundDaemonSet(
		CiliumExemptName, namespace,
		"Cilium chaining drops forwarded traffic from gateway pods",
		"Cilium stops enforcing source-IP verification on kube-ovn gateway "+
			"endpoints, or the cluster no longer chains Cilium — then delete "+
			"this and check `cilium-dbg monitor --type drop` for "+
			"'Invalid source ip' on the gateway.",
	)
	privileged := true
	hostPathDir := corev1.HostPathDirectory
	ds.Spec.Template.Spec.Containers = []corev1.Container{{
		Name: "exempt",
		// The agent image ships cilium-dbg and talks to the local agent over
		// its socket — no API access needed.
		Image:           image,
		SecurityContext: &corev1.SecurityContext{Privileged: &privileged},
		Env: []corev1.EnvVar{{
			Name: "CILIUM_SOCK", Value: "/var/run/cilium/cilium.sock",
		}},
		Command: []string{"/bin/sh", "-c"},
		Args:    []string{CiliumExemptScript},
		VolumeMounts: []corev1.VolumeMount{{
			Name:      "cilium-run",
			MountPath: "/var/run/cilium",
		}},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("10m"),
				corev1.ResourceMemory: resource.MustParse("32Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
		},
	}}
	ds.Spec.Template.Spec.Volumes = []corev1.Volume{{
		Name: "cilium-run",
		VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{
			Path: "/var/run/cilium",
			Type: &hostPathDir,
		}},
	}}
	return ds
}

func workaroundDaemonSet(name, namespace, reason, removeWhen string) *appsv1.DaemonSet {
	labels := map[string]string{
		ManagedLabel:    "true",
		"app":           name,
		WorkaroundLabel: "true",
	}
	return &appsv1.DaemonSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
			Labels:    labels,
			Annotations: map[string]string{
				WorkaroundReason:     reason,
				WorkaroundRemoveWhen: removeWhen,
			},
		},
		Spec: appsv1.DaemonSetSpec{
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": name}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": name}},
				Spec: corev1.PodSpec{
					HostNetwork: true,
					Tolerations: []corev1.Toleration{{Operator: corev1.TolerationOpExists}},
				},
			},
		},
	}
}

// ProviderNetworkName and friends apply the defaults in one place, so a CR
// written without them renders the same objects the wizard used to.
func ProviderNetworkName(u *platformv1alpha1.ManagedUnderlay) string {
	return orDefault(u.Spec.ProviderNetworkName, "external")
}

func VLANName(u *platformv1alpha1.ManagedUnderlay) string {
	return orDefault(u.Spec.VLANName, "vlan-external")
}

func SubnetName(u *platformv1alpha1.ManagedUnderlay) string {
	return orDefault(u.Spec.SubnetName, "ext-sub")
}

func orDefault(v, fallback string) string {
	if strings.TrimSpace(v) == "" {
		return fallback
	}
	return v
}

func newKubeOVN(kind, name string) *unstructured.Unstructured {
	obj := &unstructured.Unstructured{Object: map[string]any{}}
	obj.SetAPIVersion(kubeOVNGroup + "/" + kubeOVNVersion)
	obj.SetKind(kind)
	obj.SetName(name)
	return obj
}

func toAnySlice(in []string) []any {
	out := make([]any, 0, len(in))
	for _, v := range in {
		out = append(out, v)
	}
	return out
}
