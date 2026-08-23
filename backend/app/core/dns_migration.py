"""Withdrawing a resolver address this backend invented.

For a while the product computed the address of a VPC resolver — the cluster's
service CIDR with 200 in the last octet — and wrote it onto every network it
created, as `spec.dnsServer`. Nothing ever checked that anything answers there.
On a cluster where kube-ovn's vpc-dns is not enabled, nothing does: it is a
ClusterIP, and a ClusterIP has no route from inside a VPC. Every guest in every
VPC was handed it, which is why an address works from those machines and a name
does not.

The invention is gone. The networks made while it was there still carry the
declaration, and the operator renders what it is given — correctly, and now
refusing to program this particular kind of address and saying so on the
network. But the declaration is this backend's, made without being asked, and
it is this backend that withdraws it. A controller editing the spec it was
handed is a second writer of somebody else's field, which is the shape of half
the defects removed from this product in the last month.

Narrow, and on a datapath fact rather than on a memory of the formula: only an
address inside the cluster's service network, only while vpc-dns is not
enabled, only on networks this product manages. An address anywhere else may
be a resolver on the VLAN or a public one, and is somebody's deliberate
choice.

Idempotent, and it runs once at startup. There is nothing to do on a cluster
that never had the invented address, and nothing to undo on one where somebody
has since set a real resolver.
"""

import ipaddress
import logging
from typing import Any

from kubernetes_asyncio import client

from app.core.operator import OPERATOR_GROUP, OPERATOR_VERSION

logger = logging.getLogger(__name__)


async def withdraw_unreachable_dns_servers(k8s: Any) -> list[str]:
    """Clear `spec.dnsServer` where nothing can answer on it. Returns the names."""
    from app.api.v1.network import _find_kubeovn_namespace
    from app.api.v1.tenants_common import _ensure_cluster_config, _host_service_cidr

    try:
        kubeovn_ns = await _find_kubeovn_namespace(k8s)
        if kubeovn_ns:
            try:
                await k8s.core_api.read_namespaced_config_map(
                    name="vpc-dns-config", namespace=kubeovn_ns,
                )
                # The feature is on: the address may well be the real one.
                return []
            except Exception:
                pass

        await _ensure_cluster_config(k8s)
        service_cidr = _host_service_cidr()
        if not service_cidr:
            return []
        services = ipaddress.ip_network(service_cidr, strict=False)
    except Exception as e:
        logger.debug(f"Not withdrawing any dnsServer: {e}")
        return []

    custom_api = client.CustomObjectsApi(k8s._api_client)
    try:
        networks = await custom_api.list_cluster_custom_object(
            group=OPERATOR_GROUP, version=OPERATOR_VERSION, plural="managednetworks",
        )
    except Exception as e:
        logger.debug(f"No ManagedNetworks to check: {e}")
        return []

    withdrawn: list[str] = []
    for item in networks.get("items", []):
        name = (item.get("metadata") or {}).get("name", "")
        declared = ((item.get("spec") or {}).get("dnsServer") or "").strip()
        if not name or not declared:
            continue
        try:
            if ipaddress.ip_address(declared) not in services:
                continue
        except ValueError:
            continue
        try:
            await custom_api.patch_cluster_custom_object(
                group=OPERATOR_GROUP, version=OPERATOR_VERSION,
                plural="managednetworks", name=name,
                body={"spec": {"dnsServer": None}},
                _content_type="application/merge-patch+json",
            )
        except Exception as e:
            logger.warning(f"Could not withdraw dnsServer on {name}: {e}")
            continue
        logger.info(
            "Withdrew dnsServer %s from network %s: it is inside the cluster "
            "service network and kube-ovn's vpc-dns is not enabled, so nothing "
            "answers on it from inside that VPC. This backend wrote it; this "
            "backend takes it back.", declared, name,
        )
        withdrawn.append(name)
    return withdrawn
