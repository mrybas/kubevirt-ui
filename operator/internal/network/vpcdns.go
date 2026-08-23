/*
Package network, the VPC resolver's prerequisites.

kube-ovn's VpcDns gives each VPC its own CoreDNS deployment behind one
cluster-wide VIP. The controller that builds it does nothing until four things
exist, and all four are this product's to create — which is the part that did
not survive the handover.

The backend's VPC-create path made them and still does; the operator's path
made the VpcDns object and not the ground under it. So a VPC created through
the operator had the VIP handed to its guests over DHCP and nothing answering
on it: an address resolves, a name does not. Measured on the stand, and then
misread by me twice — first as "the platform does not have this feature", then
as "the product invented an address". It has the feature, and the address is
ours to pick.

What has to be there:

  - NetworkAttachmentDefinition `ovn-nad` in `default` — the secondary NIC
    each VpcDns pod gets into the cluster overlay.
  - ConfigMap `vpc-dns-config` in the kube-ovn namespace — the gate
    (`enable-vpc-dns`), the VIP, the NAD to use, and the one host the pod is
    given a route to.
  - ConfigMap `vpc-dns-corefile` — mounted by every VpcDns pod in the cluster.
  - a Kyverno ClusterPolicy per VPC, which is what actually reaches a guest:
    with bridge binding the guest is served DHCP by its launcher pod and gets
    that pod's resolver, so the pod is what has to be told.

Two of those carry a hard-won detail apiece, and both are repeated here rather
than rediscovered: `k8s-service-host` decides the single /32 route on the
pod's secondary NIC, so it must name the DNS the Corefile forwards to and not
the API server; and the Corefile forwards everything to the cluster's own
CoreDNS, because a VpcDns pod lives in the kube-ovn namespace, is not steered
through any VPC egress gateway, and has no path to the internet of its own.
*/
package network

import "fmt"

// VPCDNSConfigMap is where kube-ovn's vpc-dns controller is configured.
const VPCDNSConfigMap = "vpc-dns-config"

// VPCDNSCorefileConfigMap is mounted by every VpcDns pod in the cluster, so a
// change to it reaches every VPC at once. CoreDNS's `reload` picks it up
// without restarting anything.
const VPCDNSCorefileConfigMap = "vpc-dns-corefile"

// VPCDNSNADName is the attachment each VpcDns pod uses for its second NIC.
const VPCDNSNADName = "ovn-nad"

// VPCDNSNADProvider is how kube-ovn names that attachment on a pod.
const VPCDNSNADProvider = "ovn-nad.default.ovn"

// VPCDNSNADConfig is the CNI configuration of the shared attachment.
func VPCDNSNADConfig() string {
	return `{"cniVersion":"0.3.1","type":"kube-ovn",` +
		`"server_socket":"/run/openvswitch/kube-ovn-daemon.sock",` +
		`"provider":"` + VPCDNSNADProvider + `"}`
}

// VPCDNSVIPFor is the address this product's VPC CoreDNS answers on: the
// service network's own address with 200 in the last octet.
//
// A convention, fixed, and worth saying so — it is not discovered from
// anything and there is nothing to discover it from. kube-ovn expects to be
// told which VIP its per-VPC CoreDNS uses; this is what the product has always
// told it, and an installation that wants another one sets it explicitly.
//
// Reading it back out of the configuration this writes is a circle, and that
// circle is what produced `coredns-vip: ""`: the value was read from the file
// it is written to, so once the derivation was removed the product recorded
// its own ignorance in its own source of truth, and kube-ovn answered every
// VpcDns with "corednsVip should be set".
func VPCDNSVIPFor(serviceCIDRAddr [4]byte) string {
	return fmt.Sprintf("%d.%d.%d.200",
		serviceCIDRAddr[0], serviceCIDRAddr[1], serviceCIDRAddr[2])
}

// VPCDNSConfig is the gate configuration, given the VIP the product picked and
// the resolver its CoreDNS forwards to.
//
// Never called with an empty VIP: a file saying `enable-vpc-dns: true` with no
// address describes neither an enabled feature nor a disabled one, and it is
// what kube-ovn spins on. The callers check.
//
// `k8s-service-host` is not decoration: kube-ovn turns it into the one /32
// route it puts on the pod's secondary NIC. Left at its default — the API
// server — the pod can reach the API and nothing else, so every forward in the
// Corefile times out and each query comes back SERVFAIL. Naming the forward
// target here is what makes the route and the Corefile agree.
func VPCDNSConfig(vip, forwardDNS string) map[string]string {
	return map[string]string{
		"coredns-vip":      vip,
		"enable-vpc-dns":   "true",
		"nad-name":         VPCDNSNADName,
		"nad-provider":     VPCDNSNADProvider,
		"k8s-service-host": forwardDNS,
		"k8s-service-port": "53",
	}
}

// VPCDNSCorefile is the CoreDNS configuration every VpcDns pod runs.
//
// One forward, for everything. The public half used to go straight to a public
// resolver and timed out for a structural reason: the pod runs in the kube-ovn
// namespace, is not selected by any VPC egress gateway, and therefore has no
// route to the internet. The cluster's own CoreDNS is reachable — over the
// route `k8s-service-host` creates — and already resolves the public internet,
// so a single forward covers both zones and drops the egress dependency.
//
// The target is a ClusterIP rather than CoreDNS pod addresses, which would
// work until the next reschedule and then fail silently.
func VPCDNSCorefile(forwardDNS string) string {
	return fmt.Sprintf(`.:53 {
    errors
    health
    ready
    cache 30
    forward . %s {
        prefer_udp
        policy round_robin
    }
    loop
    reload
    loadbalance
}
`, forwardDNS)
}

// VPCDNSPolicyName is the Kyverno policy that injects the VIP into pods of one
// VPC.
func VPCDNSPolicyName(vpc string) string {
	return "kubevirt-ui-vpc-dns-" + vpc
}
