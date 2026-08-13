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
import secrets
import string
from typing import Any, Literal

from kubernetes_asyncio.client import ApiException

logger = logging.getLogger(__name__)

TALOS_TRUSTD_PORT = 50001

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
TALOS_GOLDEN_IMAGE_URL = (
    "https://factory.talos.dev/image/"
    "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"
    "/v1.13.8/openstack-amd64.raw.xz"
)

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


def worker_endpoint(tenant: str, namespace: str, api_port: int) -> str:
    """Control-plane endpoint for a Talos worker — a NAME, never an address.

    This is what makes SNI possible, and therefore what lets tenants share
    one listener on port 50001. An IP endpoint sends no SNI at all, so the
    ingress has nothing to match `HostSNI` against.
    """
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

def build_talos_pki(tenant: str, namespace: str) -> list[dict[str, Any]]:
    """cert-manager chain for the CSR signer: selfSigned → CA → signer cert.

    **DNS SANs only, no IP SANs.** Not cosmetic: an IP SAN can only be filled
    in once the address is known, which means patching the certificate after
    the fact — and a changed certificate then requires restarting the control
    plane, because the signer reads its certificate once at startup and never
    watches the file. A `rollout restart` will not do it either, since Kamaji
    owns that Deployment and reverts the annotation. Issuing DNS SANs up front
    removes the ordering dependency entirely.

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
                # DNS only — see the docstring.
                "dnsNames": dns_names,
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
    *,
    shared_vip: bool,
) -> dict[str, Any]:
    """Fragments to merge into the KamajiControlPlane spec.

    `service.additionalPorts` carries 50001 only when each tenant has its own
    VIP. On a shared VIP it must NOT go there: MetalLB refuses identical ports
    on one shared address, so the router fronts a per-tenant ClusterIP service
    instead.
    """
    additions: dict[str, Any] = {
        "additionalContainers": [build_signer_sidecar(tenant, signer_image)],
        "additionalVolumes": build_signer_volume(tenant),
        # The apiserver certificate must answer to the same names the worker
        # dials, or the join fails TLS before trustd is ever reached.
        "certSANs": signer_dns_names(tenant, namespace),
    }
    if not shared_vip:
        additions["additionalPorts"] = [{
            "name": "trustd",
            "port": TALOS_TRUSTD_PORT,
            "targetPort": TALOS_TRUSTD_PORT,
            "protocol": "TCP",
        }]
    return additions


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
) -> dict[str, Any]:
    """Talos machine config for a worker node.

    Three settings carry the design:

    * the endpoint is a **name**, which is what produces SNI and therefore
      what lets tenants share one listener on port 50001;
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
            },
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
                "endpoint": worker_endpoint(tenant, namespace, api_port),
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


def build_bootstrap_provider(namespace: str = TALOS_PROVIDER_FALLBACK_NS) -> dict[str, Any]:
    """The `BootstrapProvider talos` CR.

    A thin object consumed by capi-operator: creating it is how you ask the
    operator to install CABPT, which is what turns a TalosConfigTemplate into
    actual bootstrap data. Without CABPT the template is accepted by nobody —
    the Machine sits at WaitingForBootstrapData forever and no VM is built.
    """
    return {
        "apiVersion": f"{CAPI_OPERATOR_GROUP}/{CAPI_OPERATOR_VERSION}",
        "kind": "BootstrapProvider",
        "metadata": {
            "name": "talos",
            "namespace": namespace,
            "labels": dict(MANAGED_LABEL),
        },
        "spec": {},
    }


async def ensure_talos_bootstrap_provider(k8s) -> str:
    """Ask capi-operator to install CABPT, idempotently.

    The only cluster-wide object Talos support needs. Everything else is
    per-tenant: the CSR signer is exposed by the same TLS-passthrough route
    the apiserver already uses, one object per tenant, created and deleted
    with it.

    Returns what happened, for the caller to log.
    """
    namespace = await find_capi_operator_namespace(k8s)
    try:
        await k8s.custom_api.create_namespaced_custom_object(
            group=CAPI_OPERATOR_GROUP, version=CAPI_OPERATOR_VERSION,
            namespace=namespace,
            plural="bootstrapproviders", body=build_bootstrap_provider(namespace),
        )
        logger.info(f"Requested CABPT via BootstrapProvider 'talos' in {namespace}")
        return "created"
    except ApiException as e:
        if e.status == 409:
            return "exists"
        if e.status == 404:
            # capi-operator absent, or its namespace missing. Not fatal here:
            # `assert_cabpt_installed` gives the caller a precise error before
            # anything is created.
            logger.warning(
                "capi-operator not present; cannot request CABPT automatically"
            )
            return "unavailable"
        logger.warning(f"Could not create BootstrapProvider: {e}")
        return f"failed: {e.reason}"


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
    size: str = "20Gi",
    storage_class: str | None = None,
) -> None:
    """Import the Talos golden image into the tenant namespace if absent."""
    url = image_url or TALOS_GOLDEN_IMAGE_URL

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
            f"is not an HTTP(S) URL — using {TALOS_GOLDEN_IMAGE_URL} instead. "
            f"A container-disk reference belongs to the cloud-init worker path.",
        )
        url = TALOS_GOLDEN_IMAGE_URL

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

    for obj in build_talos_pki(tenant, namespace):
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
            # Missing is not the same as unobtainable: capi-operator installs
            # CABPT from a BootstrapProvider object, which is something the UI
            # can ask for itself. Do that, then still refuse *this* request —
            # the provider takes a moment to come up, and half-creating a
            # tenant against a CRD that does not exist yet is exactly what
            # this check exists to prevent.
            outcome = await ensure_talos_bootstrap_provider(k8s)

            if outcome in ("created", "exists"):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Talos workers need the Talos bootstrap provider "
                        f"(CABPT), which was missing. It has been requested "
                        f"from capi-operator ({outcome}); the provider takes "
                        "under a minute to install. Retry this tenant then — "
                        "or choose Standard (cloud-init) workers to create it "
                        "now."
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
