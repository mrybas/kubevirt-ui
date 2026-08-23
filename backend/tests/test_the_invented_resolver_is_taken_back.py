"""The backend withdraws the resolver address it invented.

For a while this backend computed a VPC resolver's address — the cluster's
service CIDR with 200 in the last octet — and wrote it onto every network as
`spec.dnsServer`, unchecked. On a cluster where kube-ovn's vpc-dns is off it is
a ClusterIP with no route from inside a VPC, so every guest in every VPC was
handed a resolver that cannot answer: an address works, a name does not.

The invention is gone. The networks made while it was there still declare it,
and the operator refuses to program that kind of address and says so on the
network — but it does not edit the declaration, because a controller writing
the spec it was handed is a second writer of somebody else's field.

So the author takes it back, once, at startup. Narrow: only inside the service
network, only while vpc-dns is off, and never a resolver somebody chose.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kubernetes_asyncio.client.rest import ApiException

from app.core.dns_migration import withdraw_unreachable_dns_servers


def _network(name: str, dns: str | None) -> dict:
    return {"metadata": {"name": name}, "spec": ({"dnsServer": dns} if dns else {})}


async def _run(
    networks: list[dict], *, service_cidr: str | None = "10.96.0.0/12",
    vpc_dns_enabled: bool = False, config_error: Exception | None = None,
) -> tuple[list[str], list]:
    api = MagicMock()
    api.list_cluster_custom_object = AsyncMock(return_value={"items": networks})
    api.patch_cluster_custom_object = AsyncMock()

    k8s = MagicMock()
    if config_error is not None:
        k8s.core_api.read_namespaced_config_map = AsyncMock(side_effect=config_error)
    elif vpc_dns_enabled:
        k8s.core_api.read_namespaced_config_map = AsyncMock(
            return_value=SimpleNamespace(data={"coredns-vip": "10.96.0.200"}))
    else:
        k8s.core_api.read_namespaced_config_map = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found"))

    with (
        patch("app.api.v1.network._find_kubeovn_namespace",
              AsyncMock(return_value="o0-kube-ovn")),
        patch("app.api.v1.tenants_common._ensure_cluster_config", AsyncMock()),
        patch("app.api.v1.tenants_common._host_service_cidr",
              MagicMock(return_value=service_cidr)),
        patch("app.core.dns_migration.client.CustomObjectsApi", return_value=api),
    ):
        withdrawn = await withdraw_unreachable_dns_servers(k8s)
    return withdrawn, api.patch_cluster_custom_object.await_args_list


@pytest.mark.asyncio
class TestWhatIsTakenBack:
    async def test_the_invented_address(self) -> None:
        withdrawn, patches = await _run([_network("uat-net-vm", "10.96.0.200")])
        assert withdrawn == ["uat-net-vm"]
        assert patches[0].kwargs["body"] == {"spec": {"dnsServer": None}}

    async def test_all_three_of_them(self) -> None:
        withdrawn, _ = await _run([
            _network("uat-net-vm", "10.96.0.200"),
            _network("uat-net-t1", "10.96.0.200"),
            _network("uat-net-t2", "10.96.0.200"),
        ])
        assert withdrawn == ["uat-net-vm", "uat-net-t1", "uat-net-t2"]

    async def test_any_clusterip_not_only_the_formula(self) -> None:
        """The grounds are reachability, not a memory of what we computed."""
        withdrawn, _ = await _run([_network("n", "10.96.0.53")])
        assert withdrawn == ["n"]


@pytest.mark.asyncio
class TestWhatIsLeftAlone:
    async def test_a_resolver_somebody_chose(self) -> None:
        withdrawn, patches = await _run([_network("n", "10.199.4.53")])
        assert withdrawn == [] and patches == []

    async def test_a_network_with_no_resolver_at_all(self) -> None:
        withdrawn, patches = await _run([_network("n", None)])
        assert withdrawn == [] and patches == []

    async def test_everything_when_vpc_dns_is_enabled(self) -> None:
        """Then the address may well be the real one, and it is not ours to judge."""
        withdrawn, patches = await _run(
            [_network("n", "10.96.0.200")], vpc_dns_enabled=True)
        assert withdrawn == [] and patches == []

    async def test_everything_when_the_service_network_cannot_be_read(self) -> None:
        withdrawn, patches = await _run(
            [_network("n", "10.96.0.200")], service_cidr=None)
        assert withdrawn == [] and patches == []

    async def test_everything_when_the_question_could_not_be_asked(self) -> None:
        """A 403 or a timeout is "cannot tell", not "the feature is off".

        Treating them alike would take working resolver addresses away on a
        cluster where vpc-dns is on — the one outcome this must never produce,
        and the easiest to write by catching every exception alike.
        """
        for failure in (
            ApiException(status=403, reason="Forbidden"),
            ApiException(status=500, reason="Internal Server Error"),
            TimeoutError("the apiserver did not answer"),
        ):
            withdrawn, patches = await _run(
                [_network("n", "10.96.0.200")], config_error=failure)
            assert withdrawn == [] and patches == [], failure

    async def test_a_dnsserver_that_is_not_an_address(self) -> None:
        withdrawn, _ = await _run([_network("n", "dns.internal")])
        assert withdrawn == []


@pytest.mark.asyncio
async def test_it_is_idempotent() -> None:
    """Run twice on a cluster it has already cleaned: nothing to do."""
    withdrawn, patches = await _run([_network("n", None)])
    assert withdrawn == [] and patches == []


def test_it_runs_at_startup() -> None:
    import inspect

    from app.main import lifespan

    assert "withdraw_unreachable_dns_servers" in inspect.getsource(lifespan)
