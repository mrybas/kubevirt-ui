"""Talos-flavoured tenants.

A Kamaji tenant whose nodes run Talos needs a handful of objects nothing else
does. Talos nodes do not take a cloud-init user-data blob and join; they ask a
*trustd* signer for a certificate over port 50001, and everything here exists
to make that request reach a signer that answers it.

The pieces, in the order they matter:

  * PKI for the signer — DNS SANs only, deliberately (see `build_talos_pki`);
  * Talos secrets — generated once and never rotated (`build_talos_secrets`);
  * a kubeadm-format bootstrap token in the tenant's kube-system;
  * the signer sidecar plus port 50001 on the control-plane Service;
  * a worker machine config whose endpoint is a NAME, not an address.

Talos always dials trustd on the fixed port 50001 of whatever host is in
`cluster.controlPlane.endpoint`, and puts that host in the TLS SNI. That is
the same shape the tenant apiserver already uses — TLS passthrough matched on
HostSNI — so exposing the signer is one more per-tenant route through the
cluster ingress, not a component of its own. It is created and deleted with
the tenant, and nothing shared has to be edited or restarted.

With a VIP per tenant even that is unnecessary: port 50001 goes straight onto
that tenant's own control-plane Service. Verified 2026-08-12 — a tenant with
its own VIP signed a worker CSR with no router in the path at all, which is
also why no patched control-plane provider build is needed.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import string
from typing import Any, Literal

import yaml

from kubernetes_asyncio.client import ApiException

logger = logging.getLogger(__name__)

TALOS_TRUSTD_PORT = 50001

# Size of the imported golden image, and therefore the floor for a worker's
# root clone: CDI rejects a clone into a smaller target at admission, and the
# failure surfaces as every worker stuck with an error nobody would connect to
# a disk-size field. One constant so the two cannot drift.
DEFAULT_TALOS_GOLDEN_SIZE = "20Gi"

# Default golden image for Talos workers, from the image factory.
#
#   .../image/<schematic>/<version>/openstack-amd64.raw.xz
#
# Three parts of that URL are load-bearing:
#
#   * the schematic 3765679…b4ba is the *empty* one — no system extensions. A
#     KubeVirt worker needs none; a different set of extensions means a
#     different schematic, generated with
#     `curl -X POST --data-binary 'customization: {}' https://factory.talos.dev/schematics`.
#   * the **platform** must match the disk CAPK actually attaches, and CAPK
#     attaches `cloudInitConfigDrive` — an OpenStack config-2 disk. The
#     nocloud variant looks for a `cidata` disk instead, does not find one,
#     and drops into maintenance mode waiting for `talosctl apply-config`:
#
#         volume "platform/cidata/config" phase "waiting -> missing"
#         entering maintenance service
#
#     which reads as a worker that boots fine and never joins. `openstack` is
#     the variant that reads config-2.
#   * the Talos version pins the kubelet version with it, so it has to sit
#     inside the tenant's compatibility window (Talos 1.13 → k8s 1.31–1.36).
#
# CDI pulls the .raw.xz directly and decompresses it — no conversion step. The
# import is done once, centrally, and each worker gets a CSI clone: importing
# over HTTP into every tenant namespace would depend on DNS inside the VPC,
# which is exactly what is least reliable there.
# Kept as a name because the reasoning above is about *this* URL's shape, but
# the value now comes from the catalogue: one Talos version for everyone was
# the thing T1 exists to remove, and a constant here would be a second source
# of truth next to it.
def _default_golden_url() -> str:
    from app.core.talos_catalog import default_release

    return default_release().image_url


TALOS_GOLDEN_IMAGE_URL = _default_golden_url()

CERT_MANAGER_GROUP = "cert-manager.io"
CERT_MANAGER_VERSION = "v1"


MANAGED_LABEL = {"kubevirt-ui.io/managed": "true"}

# Alphabet for kubeadm/Talos token halves — lowercase alnum, per the format
# both trustd and the kubelet bootstrapper accept.
_TOKEN_ALPHABET = string.ascii_lowercase + string.digits


# ---------------------------------------------------------------------------
# Naming — one place, because several of these strings must match each other
# exactly (the certificate's SAN, the worker's endpoint, the ingress HostSNI).
# ---------------------------------------------------------------------------

def talos_service_name(tenant: str) -> str:
    """The Service Talos treats as the control plane.

    Not Kamaji's own Service, deliberately. Talos dials trustd on port 50001
    of whatever host is in `cluster.controlPlane.endpoint`, and Kamaji cannot
    put a second port on the Service it manages: `KamajiControlPlane.spec.
    network` has no field for it, `TenantControlPlane.spec.networkProfile`
    carries a single `port`, and a port added by hand is reconciled away in
    under a minute (measured). Cozystack forks the operator over exactly
    this.

    A Service of our own avoids all of it: same selector, both ports, nothing
    upstream has to change.
    """
    return f"{tenant}-talos"


def signer_dns_names(tenant: str, namespace: str) -> list[str]:
    """The DNS names the signer certificate must carry.

    Both forms of the Talos Service name: the worker dials the short one and
    puts it in the TLS SNI, in-cluster clients resolve the long one, and the
    certificate has to satisfy whichever is presented.
    """
    svc = talos_service_name(tenant)
    return [
        f"{svc}.{namespace}.svc",
        f"{svc}.{namespace}.svc.cluster.local",
    ]


def build_talos_service(tenant: str, namespace: str, api_port: int) -> dict[str, Any]:
    """The Service that answers on both ports Talos needs.

    6443 so the worker reaches the apiserver, 50001 so it reaches trustd —
    one host, because Talos derives the second from the first and will not be
    told otherwise.
    """
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": talos_service_name(tenant),
            "namespace": namespace,
            "labels": {**MANAGED_LABEL, "kubevirt-ui.io/tenant": tenant},
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"kamaji.clastix.io/name": tenant},
            "ports": [
                {"name": "kube-apiserver", "port": api_port,
                 "targetPort": api_port, "protocol": "TCP"},
                {"name": "trustd", "port": TALOS_TRUSTD_PORT,
                 "targetPort": TALOS_TRUSTD_PORT, "protocol": "TCP"},
            ],
        },
    }


def worker_endpoint(
    tenant: str, namespace: str, api_port: int, vip: str | None = None,
) -> str:
    """Where a Talos worker looks for its control plane — and for trustd.

    Talos takes the host from here and dials trustd on its fixed :50001, so
    this single value decides both halves of the join.

    **With a VIP of its own (`vip` set), the endpoint is that address.** No
    SNI is needed because nothing is shared: the tenant owns :50001 on its
    own IP. This is also the only form a VPC worker can use — the name below
    resolves through cluster DNS to a ClusterIP, and an isolated VPC has
    neither.

    **Without one, the endpoint is a NAME**, which is what produces SNI and
    therefore what lets tenants share one listener on port 50001 behind the
    ingress. An IP endpoint sends no SNI at all, so the ingress would have
    nothing to match `HostSNI` against.
    """
    if vip:
        return f"https://{vip}:{api_port}"
    return f"https://{talos_service_name(tenant)}.{namespace}.svc:{api_port}"


# ---------------------------------------------------------------------------
# Secrets — generated once, never rotated
# ---------------------------------------------------------------------------

def _token_half(length: int) -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(length))


def generate_bootstrap_token() -> tuple[str, str]:
    """A kubeadm-format token as (id, secret): 6 chars, then 16."""
    return _token_half(6), _token_half(16)


def build_talos_secrets(tenant: str, namespace: str) -> dict[str, Any]:
    """Secret holding `machine.token`, `cluster.id` and `cluster.secret`.

    Generated once and then left alone for the tenant's whole life. Workers
    are derived from these values: rotating the token means a new worker
    cannot authenticate to the signer while the existing ones stop getting
    certificates — a failure that looks like a broken signer rather than a
    changed secret. Callers must create-if-absent and never overwrite.
    """
    token_id, token_secret = generate_bootstrap_token()
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"{tenant}-talos-secrets",
            "namespace": namespace,
            "labels": {**MANAGED_LABEL, "kubevirt-ui.io/tenant": tenant},
        },
        "type": "Opaque",
        "stringData": {
            # Also the trustd token — the two are the same value in Talos.
            "machine.token": f"{token_id}.{token_secret}",
            "cluster.id": base64.b64encode(secrets.token_bytes(32)).decode(),
            "cluster.secret": base64.b64encode(secrets.token_bytes(32)).decode(),
        },
    }


def build_bootstrap_token_secret(token_id: str, token_secret: str) -> dict[str, Any]:
    """kubeadm-format bootstrap token, for the TENANT's kube-system.

    Kamaji already creates the RBAC around it — `kubeadm:kubelet-bootstrap`
    and both auto-approvers — so only the token itself is missing.
    """
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"bootstrap-token-{token_id}",
            "namespace": "kube-system",
            "labels": dict(MANAGED_LABEL),
        },
        "type": "bootstrap.kubernetes.io/token",
        "stringData": {
            "token-id": token_id,
            "token-secret": token_secret,
            "usage-bootstrap-authentication": "true",
            "usage-bootstrap-signing": "true",
            "auth-extra-groups": "system:bootstrappers:kubeadm:default-node-token",
        },
    }



async def ensure_kubelet_bootstrap_token(k8s, tenant: str, namespace: str) -> bool:
    """Put the kubelet's bootstrap token INSIDE the tenant cluster.

    Talos hands the worker one token for two jobs: trustd authenticates the
    machine with it, and the kubelet uses the same value as a kubeadm-format
    bootstrap credential. The first half works as soon as the signer is up.
    The second needs a `bootstrap-token-<id>` Secret in the *tenant's*
    kube-system, and Kamaji creates the RBAC around it but not the Secret.

    Without it the node is a ghost: the signer issues its certificate, apid
    and the kubelet both come up healthy, and `kubectl get nodes` stays empty
    because the kubelet's TLS bootstrap has nothing to authenticate with, so
    no CSR is ever filed.

    Runs from the reconciler rather than at create time — the tenant API is
    cold until the control plane is Ready. Returns True once the token is in
    place, False while the tenant is unreachable.
    """
    from app.api.v1.tenants_storage import _build_tenant_core_api

    try:
        secrets = await read_talos_secrets(k8s, tenant, namespace)
    except ApiException:
        return False

    machine_token = secrets.get("machine.token", "")
    if "." not in machine_token:
        logger.warning(f"Tenant {tenant!r}: machine token is not id.secret")
        return False

    token_id, token_secret = machine_token.split(".", 1)
    tenant_core = await _build_tenant_core_api(k8s, tenant)
    if tenant_core is None:
        return False

    body = build_bootstrap_token_secret(token_id, token_secret)
    try:
        await tenant_core.create_namespaced_secret(namespace="kube-system", body=body)
        logger.info(f"Tenant {tenant!r}: created kubelet bootstrap token {token_id}")
        return True
    except ApiException as e:
        if e.status == 409:
            return True  # already there — the token never rotates
        logger.warning(f"Tenant {tenant!r}: bootstrap token create failed: {e}")
        return False
    finally:
        await tenant_core.api_client.close()


# ---------------------------------------------------------------------------
# PKI
# ---------------------------------------------------------------------------

def build_talos_pki(
    tenant: str, namespace: str, vip: str | None = None,
) -> list[dict[str, Any]]:
    """cert-manager chain for the CSR signer: selfSigned → CA → signer cert.

    **DNS SANs always; an IP SAN when the address is known up front.**

    The rule used to be DNS-only, for a good reason: an IP SAN could only be
    filled in after the address existed, which meant patching the certificate
    afterwards — and the signer reads its certificate once at startup and
    never watches the file, so it would also need a restart that Kamaji
    reverts (it owns the Deployment and drops the annotation).

    Per-tenant VIPs remove that ordering problem: the address is settled
    before anything is built from it, so the SAN can be issued with the
    certificate. It is required, too — a worker with its own VIP dials
    `<vip>:50001` by address, sends no SNI, and a DNS-only certificate fails
    the handshake before trustd is ever asked for anything.

    Ed25519 for the CA: small, fast, and supported by Talos's own tooling.
    """
    dns_names = signer_dns_names(tenant, namespace)
    labels = {**MANAGED_LABEL, "kubevirt-ui.io/tenant": tenant}

    def _meta(name: str) -> dict[str, Any]:
        return {"name": name, "namespace": namespace, "labels": labels}

    return [
        {
            "apiVersion": f"{CERT_MANAGER_GROUP}/{CERT_MANAGER_VERSION}",
            "kind": "Issuer",
            "metadata": _meta(f"{tenant}-talos-selfsigned"),
            "spec": {"selfSigned": {}},
        },
        {
            "apiVersion": f"{CERT_MANAGER_GROUP}/{CERT_MANAGER_VERSION}",
            "kind": "Certificate",
            "metadata": _meta(f"{tenant}-talos-ca"),
            "spec": {
                "isCA": True,
                "commonName": f"{tenant}-talos-ca",
                "secretName": f"{tenant}-talos-ca",
                # Ten years: this CA is the tenant's identity anchor and
                # rotating it means re-provisioning every node.
                "duration": "87600h",
                "renewBefore": "720h",
                "privateKey": {"algorithm": "Ed25519"},
                "issuerRef": {
                    "name": f"{tenant}-talos-selfsigned",
                    "kind": "Issuer",
                    "group": CERT_MANAGER_GROUP,
                },
            },
        },
        {
            "apiVersion": f"{CERT_MANAGER_GROUP}/{CERT_MANAGER_VERSION}",
            "kind": "Issuer",
            "metadata": _meta(f"{tenant}-talos-ca-issuer"),
            "spec": {"ca": {"secretName": f"{tenant}-talos-ca"}},
        },
        {
            "apiVersion": f"{CERT_MANAGER_GROUP}/{CERT_MANAGER_VERSION}",
            "kind": "Certificate",
            "metadata": _meta(f"{tenant}-talos-signer"),
            "spec": {
                "secretName": f"{tenant}-talos-signer",
                "duration": "8760h",
                "renewBefore": "720h",
                "privateKey": {"algorithm": "Ed25519"},
                # DNS always; the address too when the tenant owns one.
                "dnsNames": dns_names,
                **({"ipAddresses": [vip]} if vip else {}),
                "issuerRef": {
                    "name": f"{tenant}-talos-ca-issuer",
                    "kind": "Issuer",
                    "group": CERT_MANAGER_GROUP,
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Control-plane additions
# ---------------------------------------------------------------------------

def build_signer_sidecar(tenant: str, signer_image: str) -> dict[str, Any]:
    """The talos-csr-signer container that runs beside the apiserver.

    Flag names are the image's, not ours — `talos-csr-signer --help`:

        --ca-cert-path, --ca-key-path, --port, --talos-token,
        --tls-cert-path, --tls-key-path

    Getting one wrong is not a subtle failure: the binary exits with
    `unknown flag: --listen` and the container crash-loops, while the tenant
    itself stays Ready and the worker waits for a certificate forever.

    Three secrets, not one. The server certificate proves the signer's
    identity to the worker; the CA **key** is what actually signs the CSR
    (mounting only the CA certificate leaves it unable to issue anything);
    and the Talos machine token is how a worker proves it belongs to this
    tenant at all.
    """
    return {
        "name": "talos-csr-signer",
        "image": signer_image,
        "args": [
            f"--port={TALOS_TRUSTD_PORT}",
            "--tls-cert-path=/etc/talos-signer/tls.crt",
            "--tls-key-path=/etc/talos-signer/tls.key",
            "--ca-cert-path=/etc/talos-ca/tls.crt",
            "--ca-key-path=/etc/talos-ca/tls.key",
        ],
        "env": [{
            "name": "TALOS_TOKEN",
            "valueFrom": {"secretKeyRef": {
                "name": f"{tenant}-talos-secrets",
                "key": "machine.token",
            }},
        }],
        "ports": [{"name": "trustd", "containerPort": TALOS_TRUSTD_PORT}],
        "volumeMounts": [
            {
                "name": "talos-signer-certs",
                "mountPath": "/etc/talos-signer",
                "readOnly": True,
            },
            {
                "name": "talos-ca-certs",
                "mountPath": "/etc/talos-ca",
                "readOnly": True,
            },
        ],
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"memory": "128Mi"},
        },
    }


def build_signer_volume(tenant: str) -> list[dict[str, Any]]:
    """Both secrets the signer mounts: its server certificate and the CA."""
    return [
        {
            "name": "talos-signer-certs",
            "secret": {"secretName": f"{tenant}-talos-signer"},
        },
        {
            "name": "talos-ca-certs",
            "secret": {"secretName": f"{tenant}-talos-ca"},
        },
    ]


def talos_control_plane_additions(
    tenant: str,
    namespace: str,
    signer_image: str,
) -> dict[str, Any]:
    """Fragments to merge into the KamajiControlPlane spec.

    There is deliberately no port here any more. This used to return an
    `additionalPorts` entry for trustd, and `KamajiControlPlane.spec.network`
    **has no such field**: its schema is advertiseAddress, certSANs,
    dnsServiceIPs, gateway, ingress, loadBalancerConfig, serviceAddress,
    serviceAnnotations, serviceLabels, serviceType — and nothing else, so the
    API server pruned it in silence. Confirmed on two live tenants, whose
    network carries exactly advertiseAddress, certSANs and serviceType.

    It never mattered because trustd is published by the tenant's own Services
    — the LoadBalancer on its address and the ClusterIP beside it — which is
    where workers actually reach it. But a write that reads like configuration
    and does nothing is worse than no write, and four tests were guarding it.
    """
    return {
        "additionalContainers": [build_signer_sidecar(tenant, signer_image)],
        "additionalVolumes": build_signer_volume(tenant),
        # The apiserver certificate must answer to the same names the worker
        # dials, or the join fails TLS before trustd is ever reached.
        "certSANs": signer_dns_names(tenant, namespace),
    }


# ---------------------------------------------------------------------------
# Worker machine config
# ---------------------------------------------------------------------------

def build_talos_worker_config(
    tenant: str,
    namespace: str,
    *,
    api_port: int,
    control_plane_vip: str,
    machine_token: str,
    cluster_id: str,
    cluster_secret: str,
    pod_cidr: str,
    service_cidr: str,
    ca_cert_b64: str = "",
    k8s_ca_cert_b64: str = "",
    kubernetes_version: str = "",
    own_vip: str | None = None,
    nameservers: list[str] | None = None,
    time_servers: list[str] | None = None,
) -> dict[str, Any]:
    """Talos machine config for a worker node.

    Three settings carry the design:

    * the endpoint is the tenant's **own address** when it has one, and a
      **name** otherwise. The name is what produces SNI, and SNI is only
      needed while tenants share one listener on port 50001. A VPC worker
      can use nothing but the address: the name resolves through cluster DNS
      to a ClusterIP, and an isolated VPC has neither;
    * `extraHostEntries` pins that name to the control-plane VIP inside the
      node itself, so joining needs no working DNS — which matters because
      the node has none until it has joined;
    * `kubePrism` is off. It proxies the apiserver via localhost and would
      bypass the name, taking the SNI with it.

    Discovery registries are disabled: the Kubernetes one needs credentials
    the node does not have yet, and the service one talks to an external
    endpoint a tenant network may not reach.
    """
    config: dict[str, Any] = {
        "version": "v1alpha1",
        "machine": {
            "type": "worker",
            "token": machine_token,
            "network": {
                "extraHostEntries": [{
                    "ip": control_plane_vip,
                    "aliases": signer_dns_names(tenant, namespace),
                }],
                # Measured, not assumed: with bridge binding KubeVirt runs the
                # guest's DHCP itself and hands over the *launcher pod's*
                # resolv.conf — the host cluster's kube-dns, which a VPC cannot
                # reach. kube-ovn's own subnet DHCP option never gets there. The
                # node then resolves nothing, NTP fails, and Talos parks the
                # kubelet on "Waiting for time sync" with no error naming DNS.
                # The cloud-init branch works around the same thing by writing
                # resolv.conf; Talos has no cloud-init, so it goes here.
                **({"nameservers": nameservers} if nameservers else {}),
            },
            # T22. Talos will not start the kubelet until the clock is
            # synchronised, and it says so in a way that reads as a network
            # fault: "waiting for time sync", nothing naming the clock. With
            # the default public pool that makes joining a soft dependency of
            # the internet plane — the one property B3 exists to remove. The
            # tenant's own VIP is first in the list and is reachable before
            # any gateway exists; the public servers stay behind it for the
            # deployments where egress is never in question.
            **({"time": {"servers": time_servers}} if time_servers else {}),
            "features": {"kubePrism": {"enabled": False}},
            "kubelet": {
                # Without rotation the kubelet's client certificate expires
                # and the node silently stops being able to talk to the API.
                "extraArgs": {"rotate-certificates": "true"},
                # Pinned to the tenant's Kubernetes version, not left to the
                # image. Talos ships whatever kubelet matches ITS release —
                # 1.13.8 carries v1.36.2 — and against a v1.32 control plane
                # that is four minors of skew: the node boots, apid and the
                # kubelet both report healthy, and it never registers.
                **({"image": f"ghcr.io/siderolabs/kubelet:{kubernetes_version}"}
                   if kubernetes_version else {}),
            },
        },
        "cluster": {
            "id": cluster_id,
            "secret": cluster_secret,
            # The kubelet's bootstrap credential, and a different field from
            # `machine.token` above: that one authenticates the machine to
            # trustd, this one is what Talos writes into the kubelet's
            # bootstrap-kubeconfig. Setting only the first leaves the kubelet
            # with nothing to present, and it exits on a loop with
            #
            #   No valid client certificate is found and the server is
            #   responsive, exiting.
            #
            # while the node never files a CSR. Same value on purpose — it is
            # the token the `bootstrap-token-<id>` Secret in the tenant's
            # kube-system carries.
            "token": machine_token,
            "controlPlane": {
                "endpoint": worker_endpoint(tenant, namespace, api_port, own_vip),
            },
            "network": {
                "podSubnets": [pod_cidr],
                "serviceSubnets": [service_cidr],
            },
            "discovery": {
                "enabled": True,
                "registries": {
                    "kubernetes": {"disabled": True},
                    "service": {"disabled": True},
                },
            },
        },
    }
    if ca_cert_b64:
        # Talos refuses a config with neither, and says so before it ever
        # dials trustd:
        #
        #   failed to validate config acquired via platform openstack:
        #   issuing CA or some accepted CAs are required
        #     (.machine.ca, machine.acceptedCAs)
        #
        # A worker never issues certificates — it asks the signer for one —
        # so the CA goes in without its key. `acceptedCAs` is what modern
        # Talos reads; `ca` is kept for older releases.
        config["machine"]["ca"] = {"crt": ca_cert_b64}
        config["machine"]["acceptedCAs"] = [{"crt": ca_cert_b64}]

    if k8s_ca_cert_b64:
        # A different CA from the one above, and both are needed. The machine
        # CA is Talos's own; this is the *Kubernetes* CA the kubelet must
        # trust to talk to the tenant apiserver. Without it Talos accepts the
        # config, starts, and then never brings the kubelet up:
        #
        #   secrets.KubeletController: missing accepted Kubernetes CAs
        #
        # Certificate only — a worker signs nothing.
        config["cluster"]["ca"] = {"crt": k8s_ca_cert_b64}
        config["cluster"]["acceptedCAs"] = [{"crt": k8s_ca_cert_b64}]

    return config


async def read_tenant_k8s_ca_cert(k8s, tenant: str, namespace: str) -> str:
    """The tenant cluster's Kubernetes CA certificate, base64 as Talos wants.

    Kamaji keeps it in `<tenant>-ca`; the Secret already stores it encoded, so
    the value passes through untouched.
    """
    try:
        secret = await k8s.core_api.read_namespaced_secret(
            name=f"{tenant}-ca", namespace=namespace,
        )
    except ApiException as e:
        logger.warning(f"Kubernetes CA secret for tenant {tenant!r} unreadable: {e}")
        return ""

    return (secret.data or {}).get("ca.crt", "")


async def await_tenant_k8s_ca_cert(
    k8s, tenant: str, namespace: str, attempts: int = 60, delay: float = 2.0,
) -> str:
    """The Kubernetes CA, waited for rather than sampled.

    Kamaji mints `<tenant>-ca` while this flow is still assembling the worker
    config, and the two used to race. Measured on 2026-08-19:

        09:41:30.063  read <tenant>-ca -> 404, branch silently skipped
        09:41:30      TalosConfigTemplate written without cluster.ca
        09:41:31      Kamaji created the secret

    One second late, and permanently so: the template is immutable, nothing
    re-renders it, and Talos then fails every boot with

        secrets.KubeletController: missing accepted Kubernetes CAs

    at twelve seconds in — before any network, so it reads like a mystery
    rather than a race. Reproduced deterministically on a second tenant two
    hours later.

    Waiting is the fix rather than retrying the write, because the write is
    what cannot be taken back.
    """
    import asyncio

    for _ in range(attempts):
        cert = await read_tenant_k8s_ca_cert(k8s, tenant, namespace)
        if cert:
            return cert
        await asyncio.sleep(delay)

    logger.error(
        f"Kubernetes CA for tenant {tenant!r} never appeared in "
        f"{namespace}/{tenant}-ca after {attempts * delay:.0f}s"
    )
    return ""


async def read_talos_ca_cert(k8s, tenant: str, namespace: str) -> str:
    """The tenant's Talos CA certificate, base64 as Talos wants it.

    cert-manager already stores it base64-encoded in the Secret, so the value
    is passed through untouched. Missing is not fatal here — the caller omits
    the field and Talos reports the omission itself.
    """
    try:
        secret = await k8s.core_api.read_namespaced_secret(
            name=f"{tenant}-talos-ca", namespace=namespace,
        )
    except ApiException as e:
        logger.warning(f"Talos CA secret for tenant {tenant!r} unreadable: {e}")
        return ""

    return (secret.data or {}).get("tls.crt", "")


# ---------------------------------------------------------------------------
# CAPI bootstrap
# ---------------------------------------------------------------------------

CABPT_GROUP = "bootstrap.cluster.x-k8s.io"
CABPT_VERSION = "v1alpha3"
CABPT_PLURAL = "talosconfigtemplates"


def build_talos_config_template(
    tenant: str, namespace: str, machine_config_yaml: str,
) -> dict[str, Any]:
    """TalosConfigTemplate carrying a fully-rendered machine config.

    `generateType: none` on purpose: we already know every value that goes
    in — the endpoint name, the host entry, the token — and several of them
    have to match objects created elsewhere in this flow exactly. Letting
    CABPT generate the config instead would mean reconciling two sources of
    truth for the same strings.
    """
    return {
        "apiVersion": f"{CABPT_GROUP}/{CABPT_VERSION}",
        "kind": "TalosConfigTemplate",
        "metadata": {
            "name": f"{tenant}-workers",
            "namespace": namespace,
            "labels": {**MANAGED_LABEL, "kubevirt-ui.io/tenant": tenant},
        },
        "spec": {
            "template": {
                "spec": {
                    "generateType": "none",
                    "data": machine_config_yaml,
                },
            },
        },
    }


def talos_bootstrap_ref(tenant: str) -> dict[str, str]:
    """`bootstrap.configRef` for a Talos MachineDeployment."""
    return {
        "apiVersion": f"{CABPT_GROUP}/{CABPT_VERSION}",
        "kind": "TalosConfigTemplate",
        "name": f"{tenant}-workers",
    }


# ---------------------------------------------------------------------------
# Cluster singletons — one per cluster, meaningless without the feature, so
# ensured on demand exactly like the VpcDns prerequisites.
# ---------------------------------------------------------------------------

CAPI_OPERATOR_GROUP = "operator.cluster.x-k8s.io"
CAPI_OPERATOR_VERSION = "v1alpha2"


TALOS_PROVIDER_FALLBACK_NS = "capi-talos-bootstrap-system"


async def find_capi_operator_namespace(k8s) -> str:
    """Where capi-operator expects provider objects on THIS cluster.

    The namespace is a deployment choice, not a constant: the operator watches
    provider CRs wherever they are, and a cluster that installs it under a
    prefix (`o0-capi` here) keeps its providers there too. Hardcoding
    `capi-talos-bootstrap-system` means the create lands in a namespace that
    does not exist, 404s, and Talos support silently reports itself
    "unavailable" while a perfectly healthy capi-operator sits next door.

    Follows the cluster's own convention — the namespace of a provider it
    already has — and falls back to the operator's own namespace.
    """
    try:
        existing = await k8s.custom_api.list_cluster_custom_object(
            group=CAPI_OPERATOR_GROUP, version=CAPI_OPERATOR_VERSION,
            plural="bootstrapproviders",
        )
        for item in existing.get("items", []):
            ns = item.get("metadata", {}).get("namespace")
            if ns:
                return ns
    except ApiException as e:
        logger.warning(f"Could not list BootstrapProviders: {e}")

    try:
        deployments = await k8s.apps_api.list_deployment_for_all_namespaces(
            label_selector="control-plane=controller-manager",
        )
        for dep in deployments.items or []:
            if "capi-operator" in (dep.metadata.name or ""):
                return dep.metadata.namespace
    except ApiException as e:
        logger.warning(f"Could not locate the capi-operator deployment: {e}")

    return TALOS_PROVIDER_FALLBACK_NS


async def read_talos_secrets(k8s, tenant: str, namespace: str) -> dict[str, str]:
    """Read back the tenant's Talos secrets, decoded.

    Read rather than regenerated on purpose: these values are fixed for the
    tenant's whole life, and generating a second set here would hand the
    workers a token the signer does not know.
    """
    secret = await k8s.core_api.read_namespaced_secret(
        name=f"{tenant}-talos-secrets", namespace=namespace,
    )
    data = secret.data or {}
    return {
        key: base64.b64decode(value).decode("utf-8")
        for key, value in data.items()
    }


def resolve_talos_release(k8s_version: str, talos_version: str | None):
    """The catalogue entry a create request resolves to, or a 422 that lists
    what would have worked.

    This is the validator half of T1. It shares `is_compatible()` with the
    endpoint the wizard renders, so the wizard cannot offer a pair this
    refuses — the failure this codebase keeps meeting is exactly that gap.
    """
    from fastapi import HTTPException

    from app.core.talos_catalog import (
        catalog, default_release, find, is_compatible,
    )

    release = find(talos_version) if talos_version else default_release()
    if release is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Talos {talos_version} is not in this deployment's catalogue. "
                f"Offered: {', '.join(e.talos for e in catalog())}."
            ),
        )
    if not is_compatible(release, k8s_version):
        pairs = ", ".join(
            f"Talos {e.talos} -> Kubernetes {e.k8s_min}-{e.k8s_max}"
            for e in catalog()
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Talos {release.talos} does not support Kubernetes "
                f"{k8s_version} (it takes {release.k8s_min}-{release.k8s_max}). "
                f"Compatible pairs: {pairs}."
            ),
        )
    return release


# One golden image per Talos version, for the whole cluster.
#
# It used to be one per tenant: the same bytes imported over HTTP again for
# every tenant created, and — before T3 — written into by the worker that
# mounted it. The version is the only thing that distinguishes two golden
# images, so it is the name, and the name is what makes the singleton hold:
# two tenants created at the same moment both try to create
# `talos-golden-1-13-8`, etcd admits one, and the other joins it.
GOLDEN_NAMESPACE_ENV = "TENANTS_GOLDEN_NAMESPACE"
DEFAULT_GOLDEN_NAMESPACE = "kubevirt-ui-system"


def golden_namespace() -> str:
    return os.getenv(GOLDEN_NAMESPACE_ENV) or DEFAULT_GOLDEN_NAMESPACE


def golden_storage_class() -> str | None:
    """StorageClass for the golden image.

    Meant for an erasure-coded pool: this is read-only reference data cloned
    many times, which is what EC is for, while the clones themselves live on
    replica. The lab has no EC class today (`ceph-block` is replicated), so
    that half is intent rather than something measured here — the setting
    exists so a deployment that has one can point at it without a code change.
    """
    return os.getenv("TENANTS_GOLDEN_STORAGE_CLASS") or None


def golden_name(talos_version: str) -> str:
    """Deterministic, and deliberately the catalogue key.

    T1 keyed its catalogue on the Talos version; this is the same string with
    dots flattened for DNS-1123. Catalogue and image naming join at one
    identifier instead of agreeing by convention.
    """
    return f"talos-golden-{talos_version.lstrip('v').replace('.', '-')}"


def build_shared_golden_dv(
    talos_version: str, image_url: str, size: str, storage_class: str | None,
) -> dict[str, Any]:
    """The one DataVolume every tenant of this Talos version clones from."""
    storage: dict[str, Any] = {"resources": {"requests": {"storage": size}}}
    if storage_class:
        storage["storageClassName"] = storage_class
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": golden_name(talos_version),
            "namespace": golden_namespace(),
            "labels": {
                **MANAGED_LABEL,
                "kubevirt-ui.io/talos-golden": "true",
                "kubevirt-ui.io/talos-version": talos_version,
            },
        },
        "spec": {"source": {"http": {"url": image_url}}, "storage": storage},
    }


async def ensure_shared_golden_image(
    k8s, talos_version: str, image_url: str, *,
    size: str = DEFAULT_TALOS_GOLDEN_SIZE,
    wait_seconds: float = 20.0,
) -> str:
    """Create the golden for this version, or join the one already being made.

    `create` is the compare-and-swap: etcd admits exactly one object for a
    name, so two tenants created in the same second cannot both start an
    import. The loser does not retry and does not create anything of its own —
    it waits, briefly, for the winner's import.

    The wait is bounded and not fatal. CDI's clone waits for its source
    anyway, so a golden still importing delays the first worker's disk rather
    than failing the tenant; blocking tenant creation for the length of a
    20Gi HTTP pull would be worse than the thing it guards against.

    A golden in Terminating is refused outright rather than raced: recreating
    an object whose finalizers are still running produces either a stuck
    create or a second import that the finalizer then deletes.
    """
    from fastapi import HTTPException

    name = golden_name(talos_version)
    ns = golden_namespace()
    body = build_shared_golden_dv(
        talos_version, image_url, size, golden_storage_class(),
    )

    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io", version="v1beta1", namespace=ns,
            plural="datavolumes", body=body,
        )
        logger.info("Importing the shared Talos golden %s from %s", name, image_url)
        return name
    except ApiException as e:
        if e.status != 409:
            raise

    existing = None
    try:
        existing = await k8s.custom_api.get_namespaced_custom_object(
            group="cdi.kubevirt.io", version="v1beta1", namespace=ns,
            plural="datavolumes", name=name,
        )
    except ApiException:
        pass

    if existing and (existing.get("metadata") or {}).get("deletionTimestamp"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"The shared Talos {talos_version} image ({ns}/{name}) is being "
                "deleted. Creating a tenant on it now would either block on its "
                "finalizers or start an import that the deletion then removes. "
                "Retry once it is gone."
            ),
        )

    phase = ((existing or {}).get("status") or {}).get("phase")
    if phase == "Succeeded":
        return name

    logger.info("Shared Talos golden %s already exists (phase=%s); joining it",
                name, phase)
    await _wait_for_golden(k8s, name, ns, wait_seconds)
    return name


GOLDEN_CLONER_ROLE = "talos-golden-cloner"


def build_golden_cloner_role() -> dict[str, Any]:
    """Permission to clone the shared golden, in the namespace that holds it.

    `datavolumes/source` grants nothing by itself: CDI's webhook checks that
    whoever creates a cross-namespace clone may `create` it in the **source**
    namespace, and refuses otherwise.
    """
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": GOLDEN_CLONER_ROLE,
            "namespace": golden_namespace(),
            "labels": dict(MANAGED_LABEL),
        },
        "rules": [{
            "apiGroups": ["cdi.kubevirt.io"],
            "resources": ["datavolumes/source"],
            "verbs": ["create"],
        }],
    }


def build_golden_cloner_binding(tenant_namespace: str) -> dict[str, Any]:
    """Bind that permission to the identity that actually performs the clone.

    Not our backend. The worker's root disk is a `dataVolumeTemplate` on the
    VirtualMachine, so KubeVirt creates it — and CDI evaluates the **tenant
    namespace's default ServiceAccount**, which is what the cluster said in as
    many words:

        not authorized to create DataVolume: User
        system:serviceaccount:tenant-ga:default has insufficient permissions
        in clone source namespace kubevirt-ui-system

    Granting the backend `datavolumes/source` was necessary and not
    sufficient; there are two subjects on this path and they are different.
    """
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": f"{GOLDEN_CLONER_ROLE}-{tenant_namespace}",
            "namespace": golden_namespace(),
            "labels": dict(MANAGED_LABEL),
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": GOLDEN_CLONER_ROLE,
        },
        "subjects": [{
            "kind": "ServiceAccount",
            "name": "default",
            "namespace": tenant_namespace,
        }],
    }


async def ensure_golden_clone_rbac(k8s, tenant_namespace: str) -> None:
    """Let this tenant's VMs clone the shared golden. Idempotent."""
    from kubernetes_asyncio.client import RbacAuthorizationV1Api

    rbac = RbacAuthorizationV1Api(k8s._api_client)
    ns = golden_namespace()
    for body, api in (
        (build_golden_cloner_role(), "role"),
        (build_golden_cloner_binding(tenant_namespace), "binding"),
    ):
        try:
            if api == "role":
                await rbac.create_namespaced_role(namespace=ns, body=body)
            else:
                await rbac.create_namespaced_role_binding(namespace=ns, body=body)
        except ApiException as e:
            if e.status != 409:
                logger.error(
                    "Could not grant %s the right to clone the shared golden "
                    "(%s): its workers will fail with 'insufficient permissions "
                    "in clone source namespace'. %s", tenant_namespace, api, e,
                )


async def _wait_for_golden(k8s, name: str, ns: str, seconds: float) -> None:
    import asyncio

    deadline = seconds
    while deadline > 0:
        await asyncio.sleep(2.0)
        deadline -= 2.0
        try:
            dv = await k8s.custom_api.get_namespaced_custom_object(
                group="cdi.kubevirt.io", version="v1beta1", namespace=ns,
                plural="datavolumes", name=name,
            )
        except ApiException:
            return
        if ((dv.get("status") or {}).get("phase")) == "Succeeded":
            return
    logger.info(
        "Shared Talos golden %s is still importing; the first worker's clone "
        "will wait for it", name,
    )


def build_talos_golden_dv(
    tenant: str, namespace: str, image_url: str, size: str, storage_class: str | None,
) -> dict[str, Any]:
    """DataVolume importing the Talos golden image for this tenant.

    CDI fetches the .raw.xz and decompresses it — no conversion step. Workers
    then CSI-clone this one PVC rather than each importing over HTTP, which
    inside a VPC would depend on DNS being up before the node exists.
    """
    storage: dict[str, Any] = {"resources": {"requests": {"storage": size}}}
    if storage_class:
        storage["storageClassName"] = storage_class
    return {
        "apiVersion": "cdi.kubevirt.io/v1beta1",
        "kind": "DataVolume",
        "metadata": {
            "name": f"{tenant}-talos-golden",
            "namespace": namespace,
            "labels": {
                **MANAGED_LABEL,
                "kubevirt-ui.io/tenant": tenant,
                "kubevirt-ui.io/talos-golden": "true",
            },
        },
        "spec": {"source": {"http": {"url": image_url}}, "storage": storage},
    }


async def ensure_talos_golden_image(
    k8s, tenant: str, namespace: str, *,
    image_url: str | None = None,
    size: str = DEFAULT_TALOS_GOLDEN_SIZE,
    storage_class: str | None = None,
) -> None:
    """Import the Talos golden image into the tenant namespace if absent."""
    url = image_url or _default_golden_url()

    # The wizard's worker image field is shared with the cloud-init path,
    # where it holds a CAPK *container disk* reference. CDI takes this one as
    # an HTTP source and rejects anything that is not a URL:
    #
    #   admission webhook "datavolume-validate.cdi.kubevirt.io" denied the
    #   request: spec.source Invalid source URL:
    #   quay.io/capk/ubuntu-2404-container-disk:v1.32.1
    #
    # By then the tenant's Talos secrets and PKI are already written, so the
    # failure leaves half a tenant behind. A registry reference here can only
    # be the wrong field carried over — fall back to the known-good image.
    if not url.startswith(("http://", "https://")):
        logger.warning(
            f"Talos golden image for tenant {tenant!r} was given {url!r}, which "
            f"is not an HTTP(S) URL — using {_default_golden_url()} instead. "
            f"A container-disk reference belongs to the cloud-init worker path.",
        )
        url = _default_golden_url()

    body = build_talos_golden_dv(tenant, namespace, url, size, storage_class)
    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io", version="v1beta1", namespace=namespace,
            plural="datavolumes", body=body,
        )
        logger.info(f"Importing Talos golden image for {tenant!r} from {url}")
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"Talos golden image for {tenant!r} already present")


async def ensure_talos_tenant_objects(
    k8s, tenant: str, namespace: str, api_port: int = 6443,
    vip: str | None = None,
) -> None:
    """Create the per-tenant Talos objects that must exist before the CAPI ones.

    Ordering matters: the control plane mounts the signer certificate, and the
    machine config embeds the machine token, so both have to exist before the
    KamajiControlPlane and the TalosConfigTemplate are built.

    Create-if-absent throughout. The secrets in particular must never be
    overwritten — see `build_talos_secrets`.
    """
    from kubernetes_asyncio import client as k8s_client

    core = k8s.core_api

    # The Service the worker treats as its control plane — both ports on one
    # host, because Talos derives the trustd address from the apiserver one.
    svc_body = build_talos_service(tenant, namespace, api_port)
    try:
        await core.create_namespaced_service(
            namespace=namespace, body=svc_body,
        )
        logger.info(f"Created Talos control-plane Service for tenant {tenant!r}")
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"Talos Service for tenant {tenant!r} already exists")

    secret_body = build_talos_secrets(tenant, namespace)
    try:
        await core.create_namespaced_secret(
            namespace=namespace, body=k8s_client.V1Secret(**{
                "metadata": k8s_client.V1ObjectMeta(**secret_body["metadata"]),
                "type": secret_body["type"],
                "string_data": secret_body["stringData"],
            }),
        )
        logger.info(f"Created Talos secrets for tenant {tenant!r}")
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"Talos secrets for tenant {tenant!r} already exist; keeping them")

    for obj in build_talos_pki(tenant, namespace, vip):
        plural = f"{obj['kind'].lower()}s"
        try:
            await k8s.custom_api.create_namespaced_custom_object(
                group=CERT_MANAGER_GROUP, version=CERT_MANAGER_VERSION,
                namespace=namespace, plural=plural, body=obj,
            )
        except ApiException as e:
            if e.status == 409:
                continue
            if e.status == 404:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Talos workers need cert-manager for the CSR signer's "
                        f"PKI, but {plural}.{CERT_MANAGER_GROUP} is not "
                        "installed. Install cert-manager or choose "
                        "worker_os='cloud-init'."
                    ),
                ) from e
            raise
    logger.info(f"Ensured Talos PKI for tenant {tenant!r}")


async def assert_cabpt_installed(k8s) -> None:
    """Fail early when the Talos bootstrap provider is missing.

    Without CABPT the TalosConfigTemplate is accepted by nobody: the
    MachineDeployment is created, the Machine sits at
    `WaitingForBootstrapData` forever, and no VM is ever built. That reads as
    a broken tenant rather than a missing provider, so refuse up front.
    """
    from fastapi import HTTPException

    try:
        await k8s.custom_api.list_cluster_custom_object(
            group="apiextensions.k8s.io", version="v1", plural="customresourcedefinitions",
            field_selector=f"metadata.name={CABPT_PLURAL}.{CABPT_GROUP}",
        )
    except ApiException:
        # Listing CRDs may itself be forbidden; fall through to the direct
        # probe below rather than failing on the diagnostic.
        pass

    try:
        await k8s.custom_api.list_namespaced_custom_object(
            group=CABPT_GROUP, version=CABPT_VERSION,
            namespace="default", plural=CABPT_PLURAL,
        )
    except ApiException as e:
        if e.status == 404:
            # T9: installing it is the bootstrap's job now, not this request's.
            #
            # Creating the BootstrapProvider here worked, and that was the
            # problem: it went out with an empty spec, so capi-operator
            # installed whatever release was newest that day. Two clusters
            # built a week apart ran different CABPT versions and nothing in
            # either recorded which. The provider is pinned in the
            # `capi-providers` component now and arrives with the cluster.
            #
            # Refusing with an instruction is the honest answer when it is
            # absent: the component is a one-line enable, and a tenant
            # half-created against a CRD that does not exist is exactly what
            # this check exists to prevent.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Talos workers need the Talos bootstrap provider (CABPT), "
                    "and this cluster does not have it. It ships with the "
                    "`capi-providers` component — enable `bootstrap.talos` "
                    "there and it installs at a pinned version, like every "
                    "other provider. Until then, Standard (cloud-init) "
                    "workers work on this cluster."
                ),
            )

            raise HTTPException(
                status_code=422,
                detail=(
                    "Talos workers need the Talos bootstrap provider (CABPT): "
                    f"{CABPT_PLURAL}.{CABPT_GROUP} is not installed on this "
                    "cluster, and capi-operator is not available to install it "
                    f"({outcome}). Install capi-operator and the Talos "
                    "BootstrapProvider, or choose worker_os='cloud-init'. "
                    "Without it the workers would wait for bootstrap data "
                    "forever."
                ),
            )
        # Anything else (403, transient) is not proof of absence — let the
        # create proceed rather than blocking on a diagnostic.
        logger.warning(f"Could not verify CABPT installation: {e}")


# ---------------------------------------------------------------------------
# Worker NIC binding
# ---------------------------------------------------------------------------

WorkerBinding = Literal["bridge", "masquerade"]


def validate_worker_binding(binding: WorkerBinding) -> None:
    """Talos workers must use bridge binding.

    With masquerade every guest sees itself as 10.0.2.2 and registers under
    that address: the first node joins and the second cannot, because the
    cluster already has a node claiming it. There is no partial version of
    this failure — it is one node or an error.
    """
    if binding != "bridge":
        raise ValueError(
            "Talos workers require network_binding='bridge'. With masquerade "
            "every guest sees itself as 10.0.2.2 and registers under that "
            "address, so only the first node can ever join."
        )


async def ensure_worker_bootstrap_ca(k8s) -> None:
    """Repair worker bootstrap templates that carry no Kubernetes CA.

    The wait in `await_tenant_k8s_ca_cert` makes the race unlikely; this makes
    losing it survivable. Both are needed, and only this one covers a tenant
    created before the wait existed.

    A TalosConfigTemplate is immutable, so repair means a *new* template and
    a MachineDeployment repointed at it — the same two moves that fixed the
    UAT tenants by hand. CAPI then rolls the workers itself.

    Deliberately narrow: only templates this product manages, only the
    missing-`cluster.ca` case, and only when the CA is actually available to
    write. Anything else is left alone.
    """
    try:
        namespaces = await k8s.core_api.list_namespace(
            label_selector="kubevirt-ui.io/tenant",
        )
    except ApiException as e:
        logger.error(f"worker bootstrap reconcile: cannot list tenants: {e}")
        return

    for ns_obj in namespaces.items or []:
        ns = ns_obj.metadata.name
        tenant = (ns_obj.metadata.labels or {}).get("kubevirt-ui.io/tenant")
        if not tenant:
            continue
        try:
            await _repair_one_worker_bootstrap(k8s, tenant, ns)
        except Exception as e:  # noqa: BLE001 - one tenant must not stop the rest
            logger.error(f"worker bootstrap reconcile failed for {tenant!r}: {e}")


def _template_lacks_k8s_ca(template: dict[str, Any]) -> bool:
    """True when the rendered config has no Kubernetes CA under `cluster`.

    `machine.ca` is the Talos CA and is always present; it is the *cluster*
    branch that `secrets.KubeletController` reads, and its absence is the
    whole defect.
    """
    data = (
        template.get("spec", {}).get("template", {}).get("spec", {}).get("data")
    )
    if not data:
        return False
    try:
        parsed = yaml.safe_load(data) or {}
    except yaml.YAMLError:
        return False
    cluster = parsed.get("cluster") or {}
    return not (cluster.get("ca") or cluster.get("acceptedCAs"))


async def _repair_one_worker_bootstrap(k8s, tenant: str, ns: str) -> None:
    custom = k8s.custom_api
    try:
        template = await custom.get_namespaced_custom_object(
            group=CABPT_GROUP, version=CABPT_VERSION, namespace=ns,
            plural=CABPT_PLURAL, name=f"{tenant}-workers",
        )
    except ApiException as e:
        if e.status != 404:
            raise
        return  # cloud-init tenant, or nothing built yet

    if not _template_lacks_k8s_ca(template):
        return

    ca = await read_tenant_k8s_ca_cert(k8s, tenant, ns)
    if not ca:
        logger.warning(
            f"Tenant {tenant!r}: worker template has no Kubernetes CA and the "
            f"CA secret is still absent — leaving both alone"
        )
        return

    data = template["spec"]["template"]["spec"]["data"]
    config = yaml.safe_load(data)
    config.setdefault("cluster", {})["ca"] = {"crt": ca}
    config["cluster"]["acceptedCAs"] = [{"crt": ca}]

    # A new object rather than a patch: the CRD refuses spec changes, which is
    # why the defect was permanent in the first place.
    repaired_name = f"{tenant}-workers-ca"
    body = build_talos_config_template(
        tenant, ns, yaml.safe_dump(config, default_flow_style=False, sort_keys=False),
    )
    body["metadata"]["name"] = repaired_name
    try:
        await custom.create_namespaced_custom_object(
            group=CABPT_GROUP, version=CABPT_VERSION, namespace=ns,
            plural=CABPT_PLURAL, body=body,
        )
    except ApiException as e:
        if e.status != 409:
            raise

    await custom.patch_namespaced_custom_object(
        group="cluster.x-k8s.io", version="v1beta1", namespace=ns,
        plural="machinedeployments", name=f"{tenant}-workers",
        body={"spec": {"template": {"spec": {"bootstrap": {
            "configRef": {"name": repaired_name},
        }}}}},
        _content_type="application/merge-patch+json",
    )
    logger.info(
        f"Tenant {tenant!r}: worker bootstrap template had no Kubernetes CA; "
        f"wrote {repaired_name} and repointed the MachineDeployment"
    )
