"""The one-second race that bricked a tenant, and the repair for when it wins.

Measured 2026-08-19:

    09:41:30.063  read <tenant>-ca -> 404, the branch silently skipped
    09:41:30      TalosConfigTemplate written without cluster.ca
    09:41:31      Kamaji created the secret

Talos then failed at twelve seconds into every boot with
`secrets.KubeletController: missing accepted Kubernetes CAs` — before any
network, so it reads like anything but a race. The template is immutable, so
the miss was permanent: two tenants had to be repaired by hand.

Two fixes, and both are needed. Waiting (a) makes losing the race unlikely.
Repair (b) makes it survivable — and is the only thing that helps a tenant
created before the wait existed. Testing (a) alone would leave (b) never
exercised, because when (a) works there is nothing broken to repair.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from kubernetes_asyncio.client import ApiException

from app.api.v1.tenants_talos import (
    _repair_one_worker_bootstrap,
    _template_lacks_k8s_ca,
    await_tenant_k8s_ca_cert,
)

CA = "TEST-CA-BASE64"


def _template(cluster: dict) -> dict:
    config = {
        "version": "v1alpha1",
        "machine": {"type": "worker", "ca": {"crt": "TALOS-CA"}},
        "cluster": cluster,
    }
    return {
        "spec": {"template": {"spec": {"data": yaml.safe_dump(config)}}},
    }


# ---------------------------------------------------------------------------
# (a) waiting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_ca_is_waited_for_not_sampled() -> None:
    k8s = MagicMock()
    calls = {"n": 0}

    async def _read(name, namespace):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ApiException(status=404)
        return SimpleNamespace(data={"ca.crt": CA})

    k8s.core_api.read_namespaced_secret = _read

    assert await await_tenant_k8s_ca_cert(
        k8s, "t1", "tenant-t1", attempts=5, delay=0,
    ) == CA
    assert calls["n"] == 3, "gave up before Kamaji had finished"


@pytest.mark.asyncio
async def test_a_ca_that_never_arrives_yields_nothing_rather_than_a_guess() -> None:
    k8s = MagicMock()

    async def _missing(name, namespace):
        raise ApiException(status=404)

    k8s.core_api.read_namespaced_secret = _missing

    assert await await_tenant_k8s_ca_cert(
        k8s, "t1", "tenant-t1", attempts=3, delay=0,
    ) == ""


# ---------------------------------------------------------------------------
# (b) repair
# ---------------------------------------------------------------------------

def test_a_template_with_only_the_talos_ca_is_detected() -> None:
    # `machine.ca` is always there and is a different CA entirely; reading it
    # as "has a CA" is how this would go unnoticed.
    assert _template_lacks_k8s_ca(_template({"id": "x"})) is True


def test_a_healthy_template_is_left_alone() -> None:
    assert _template_lacks_k8s_ca(_template({"ca": {"crt": CA}})) is False
    assert _template_lacks_k8s_ca(
        _template({"acceptedCAs": [{"crt": CA}]}),
    ) is False


def _k8s_with(template: dict | None, ca: str | None = CA):
    k8s = MagicMock()

    async def _get(**kw):
        if template is None:
            raise ApiException(status=404)
        return template

    async def _read_secret(name, namespace):
        if ca is None:
            raise ApiException(status=404)
        return SimpleNamespace(data={"ca.crt": ca})

    k8s.custom_api.get_namespaced_custom_object = _get
    k8s.custom_api.create_namespaced_custom_object = AsyncMock()
    k8s.custom_api.patch_namespaced_custom_object = AsyncMock()
    k8s.core_api.read_namespaced_secret = _read_secret
    return k8s


@pytest.mark.asyncio
async def test_a_bricked_tenant_is_repaired_without_a_human() -> None:
    k8s = _k8s_with(_template({"id": "x"}))

    await _repair_one_worker_bootstrap(k8s, "t1", "tenant-t1")

    created = k8s.custom_api.create_namespaced_custom_object.call_args.kwargs
    config = yaml.safe_load(created["body"]["spec"]["template"]["spec"]["data"])
    assert config["cluster"]["ca"]["crt"] == CA
    assert config["cluster"]["acceptedCAs"] == [{"crt": CA}]

    # A new object, because the CRD refuses spec changes — which is exactly
    # why the miss was permanent.
    assert created["body"]["metadata"]["name"] == "t1-workers-ca"

    patched = k8s.custom_api.patch_namespaced_custom_object.call_args.kwargs
    assert patched["plural"] == "machinedeployments"
    assert patched["body"]["spec"]["template"]["spec"]["bootstrap"]["configRef"][
        "name"] == "t1-workers-ca"


@pytest.mark.asyncio
async def test_a_healthy_tenant_is_not_rolled() -> None:
    # Repair rolls every worker. Doing that to a tenant that is fine would be
    # a worse defect than the one being fixed.
    k8s = _k8s_with(_template({"ca": {"crt": CA}}))

    await _repair_one_worker_bootstrap(k8s, "t1", "tenant-t1")

    k8s.custom_api.create_namespaced_custom_object.assert_not_called()
    k8s.custom_api.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_repair_refuses_to_write_another_template_without_the_ca() -> None:
    # Replacing one CA-less template with another would roll the workers for
    # nothing and reset the clock on the same failure.
    k8s = _k8s_with(_template({"id": "x"}), ca=None)

    await _repair_one_worker_bootstrap(k8s, "t1", "tenant-t1")

    k8s.custom_api.create_namespaced_custom_object.assert_not_called()
    k8s.custom_api.patch_namespaced_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_a_cloud_init_tenant_has_no_template_and_that_is_fine() -> None:
    k8s = _k8s_with(None)

    await _repair_one_worker_bootstrap(k8s, "t1", "tenant-t1")

    k8s.custom_api.create_namespaced_custom_object.assert_not_called()
