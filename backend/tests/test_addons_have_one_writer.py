"""Enabling an addon edits the tenant's description, not a second HelmRelease.

There were three writers of a tenant's HelmReleases: creation, the periodic
reconciler, and this endpoint. The first two are handed over under
`OPERATOR_TENANT_ADDONS_ENABLED`; this one was not, and it is the one a person
presses.

What it cost, measured on the stand: the release this endpoint writes carries
the same labels the operator's does, and the operator deletes releases carrying
those labels that the tenant's description does not mention. So the release
survived every poll for a minute — nothing watches it — and then vanished
within five seconds of the next reconcile of the ManagedTenant. A button that
works and then quietly undoes itself some minutes later.

It also appended `<tenant>-<namespace>` to the namespace list while the release
installs into `<namespace>`, which is where `uat-t1-alloy` came from: a
namespace created in the tenant's cluster that nothing ever uses.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from kubernetes_asyncio.client.exceptions import ApiException

from app.api.v1.tenants_crud import disable_addon, enable_addon
from app.models.tenant import TenantAddon


def _stand(monkeypatch, *, described: dict[str, Any] | None, handed_over: bool = True):
    """A tenant that may or may not have a description the operator acts on."""
    import app.api.v1.tenants_crud as mod

    monkeypatch.setattr(mod, "tenant_addons_path_enabled", lambda: handed_over)
    monkeypatch.setattr(mod, "require_tenant_access", AsyncMock())

    catalog = MagicMock()
    catalog.get_component.return_value = MagicMock(
        parameters=[], namespace="alloy", category="observability", required=False,
    )
    monkeypatch.setattr(mod, "_get_addon_catalog", AsyncMock(return_value=catalog))

    k8s = MagicMock()
    if described is None:
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=404),
        )
    else:
        described.setdefault("metadata", {}).setdefault("resourceVersion", "100")
        k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value=described)
    k8s.custom_api.patch_cluster_custom_object = AsyncMock()
    k8s.custom_api.create_namespaced_custom_object = AsyncMock()
    k8s.custom_api.delete_namespaced_custom_object = AsyncMock()
    k8s.custom_api.get_namespaced_custom_object = AsyncMock(
        return_value={"spec": {"values": {"namespaces": []}}},
    )
    k8s.custom_api.patch_namespaced_custom_object = AsyncMock()

    request = MagicMock()
    request.app.state.k8s_client = k8s
    return request, k8s


@pytest.mark.asyncio
async def test_enabling_writes_the_description_and_no_release(monkeypatch) -> None:
    request, k8s = _stand(monkeypatch, described={"spec": {"addons": []}})

    status = await enable_addon(
        request, "uat-t2",
        TenantAddon(addon_id="alloy", parameters={"SCRAPE_INTERVAL": "15s"}),
        user=MagicMock(),
    )

    k8s.custom_api.create_namespaced_custom_object.assert_not_awaited()
    k8s.custom_api.patch_namespaced_custom_object.assert_not_awaited()
    patch = k8s.custom_api.patch_cluster_custom_object.await_args.kwargs
    assert patch["plural"] == "managedtenants"
    assert patch["name"] == "uat-t2"
    assert patch["body"]["spec"]["addons"] == [
        {"id": "alloy", "parameters": {"SCRAPE_INTERVAL": "15s"}},
    ]
    # The version it read, so a second request that read the same list loses
    # rather than overwrites.
    assert patch["body"]["metadata"]["resourceVersion"] == "100"
    assert not status.ready


@pytest.mark.asyncio
async def test_enabling_what_is_already_described_is_the_same_conflict(monkeypatch) -> None:
    request, k8s = _stand(
        monkeypatch, described={"spec": {"addons": [{"id": "alloy"}]}},
    )

    with pytest.raises(HTTPException) as e:
        await enable_addon(request, "uat-t2", TenantAddon(addon_id="alloy"), user=MagicMock())

    assert e.value.status_code == 409
    k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabling_removes_it_from_the_description(monkeypatch) -> None:
    request, k8s = _stand(
        monkeypatch,
        described={"spec": {"addons": [{"id": "alloy"}, {"id": "kubevirt-csi-driver"}]}},
    )

    await disable_addon(request, "uat-t2", "alloy", user=MagicMock())

    k8s.custom_api.delete_namespaced_custom_object.assert_not_awaited()
    patch = k8s.custom_api.patch_cluster_custom_object.await_args.kwargs
    assert patch["body"]["spec"]["addons"] == [{"id": "kubevirt-csi-driver"}]


@pytest.mark.asyncio
async def test_a_tenant_the_operator_does_not_know_is_still_ours(monkeypatch) -> None:
    """Half a migration is the normal state, and it has to work."""
    request, k8s = _stand(monkeypatch, described=None)

    await enable_addon(request, "legacy", TenantAddon(addon_id="alloy"), user=MagicMock())

    k8s.custom_api.create_namespaced_custom_object.assert_awaited()
    k8s.custom_api.patch_cluster_custom_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_the_flag_nothing_is_read_or_described(monkeypatch) -> None:
    """The flag is the cutover, so with it off this endpoint asks no questions."""
    request, k8s = _stand(
        monkeypatch, described={"spec": {"addons": []}}, handed_over=False,
    )

    await enable_addon(request, "uat-t2", TenantAddon(addon_id="alloy"), user=MagicMock())

    k8s.custom_api.get_cluster_custom_object.assert_not_awaited()
    k8s.custom_api.create_namespaced_custom_object.assert_awaited()


@pytest.mark.asyncio
async def test_two_enables_that_read_the_same_list_do_not_lose_one(monkeypatch) -> None:
    """The whole list in one patch is the shape that loses writes.

    Two requests enable two different addons. Both read `[]`, so each would
    write a list of one and the second would erase the first — silently, with
    the UI showing success both times. The API server can only refuse it if the
    version that was read is part of the write.
    """
    described = {"metadata": {"resourceVersion": "100"}, "spec": {"addons": []}}
    request, k8s = _stand(monkeypatch, described=described)

    written: list[list[dict[str, Any]]] = []

    async def patch(**kwargs):
        body = kwargs["body"]
        seen = (body.get("metadata") or {}).get("resourceVersion")
        if seen != described["metadata"]["resourceVersion"]:
            raise ApiException(status=409, reason="Conflict")
        described["metadata"]["resourceVersion"] = str(
            int(described["metadata"]["resourceVersion"]) + 1,
        )
        described["spec"]["addons"] = body["spec"]["addons"]
        written.append(body["spec"]["addons"])

    k8s.custom_api.patch_cluster_custom_object = AsyncMock(side_effect=patch)

    await enable_addon(request, "uat-t2", TenantAddon(addon_id="alloy"), user=MagicMock())

    # The second request read the list before the first one landed.
    stale = dict(described)
    stale["metadata"] = {"resourceVersion": "100"}
    stale["spec"] = {"addons": []}
    k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value=stale)

    with pytest.raises(HTTPException) as e:
        await enable_addon(
            # Any second addon; not the CSI driver, which now wires the host
            # side of storage on its way through and would need a cluster.
            request, "uat-t2", TenantAddon(addon_id="alloy2"), user=MagicMock(),
        )

    assert e.value.status_code == 409
    assert written == [[{"id": "alloy"}]], "the second write erased the first"
    assert described["spec"]["addons"] == [{"id": "alloy"}]
