"""Unit tests for tenant teardown ordering.

The namespace delete is what actually removes the tenant (control plane,
worker VMs, volumes) — every preceding cleanup step is cluster-scoped
bookkeeping and must never be able to skip it.
"""

from types import SimpleNamespace
from app.core.auth import User


def _admin_user() -> User:
    """These tests exercise delete mechanics, not authorisation — the caller is
    a platform admin so `require_tenant_access` short-circuits."""
    return User(
        id="admin", email="admin@local", username="admin",
        groups=["kubevirt-ui-admins"],
    )


from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client import ApiException

from app.api.v1 import tenants_crud


def _managed_tenant_deletes(k8s: MagicMock) -> list[dict]:
    """The cluster-scoped deletes that were about the tenant's description.

    These assertions used to be `assert_awaited_once` on the mock itself,
    which counts every cluster-scoped delete the teardown makes — and the
    teardown grew: releasing the transit plane removes kube-ovn objects that
    are cluster-scoped too. The tests then failed for a reason unrelated to
    what they are about, and stayed red long enough to become scenery.

    What they mean is "the ManagedTenant was deleted, once".
    """
    return [
        call.kwargs for call in k8s.custom_api.delete_cluster_custom_object.await_args_list
        if call.kwargs.get("plural") == "managedtenants"
    ]


@pytest.fixture
def k8s() -> MagicMock:
    mock = MagicMock()
    mock.core_api.read_namespace = AsyncMock(return_value=MagicMock())
    mock.core_api.delete_namespace = AsyncMock()
    mock.custom_api.delete_namespaced_custom_object = AsyncMock()
    # A tenant this cluster's operator has no object for — the ordinary case
    # while the handover is off, and the same 404 an installation without the
    # CRD at all returns.
    mock.custom_api.delete_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found"),
    )
    mock.custom_api.get_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found"),
    )
    return mock


@pytest.fixture
def request_obj(k8s: MagicMock) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(k8s_client=k8s)))


@pytest.fixture(autouse=True)
def stub_cleanups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the three cluster-scoped cleanups by default."""
    for fn in (
        "_detach_tenant_ns_from_vpc_subnet",
        "delete_csi_cluster_role_binding",
        "release_cp_ports",
    ):
        monkeypatch.setattr(tenants_crud, fn, AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing",
    [
        "_detach_tenant_ns_from_vpc_subnet",
        "delete_csi_cluster_role_binding",
        "release_cp_ports",
    ],
)
async def test_namespace_is_deleted_even_if_a_cleanup_step_fails(
    request_obj: SimpleNamespace, k8s: MagicMock,
    monkeypatch: pytest.MonkeyPatch, failing: str,
) -> None:
    # `release_cp_ports` re-raises non-409/404 ApiExceptions; the other two
    # can fail on any transient apiserver error.
    monkeypatch.setattr(
        tenants_crud, failing,
        AsyncMock(side_effect=ApiException(status=500, reason="boom")),
    )

    await tenants_crud.delete_tenant(request_obj, "demo", user=_admin_user())

    k8s.core_api.delete_namespace.assert_awaited_once_with(name="tenant-demo")


@pytest.mark.asyncio
async def test_deletes_capi_cluster_before_the_namespace(
    request_obj: SimpleNamespace, k8s: MagicMock,
) -> None:
    await tenants_crud.delete_tenant(request_obj, "demo", user=_admin_user())

    k8s.custom_api.delete_namespaced_custom_object.assert_awaited_once()
    kwargs = k8s.custom_api.delete_namespaced_custom_object.await_args.kwargs
    assert kwargs["plural"] == "clusters"
    assert kwargs["name"] == "demo"
    assert kwargs["namespace"] == "tenant-demo"
    k8s.core_api.delete_namespace.assert_awaited_once_with(name="tenant-demo")


@pytest.mark.asyncio
async def test_missing_namespace_is_404(
    request_obj: SimpleNamespace, k8s: MagicMock,
) -> None:
    k8s.core_api.read_namespace = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found"),
    )

    with pytest.raises(Exception) as exc_info:
        await tenants_crud.delete_tenant(request_obj, "demo", user=_admin_user())

    assert getattr(exc_info.value, "status_code", None) == 404
    k8s.core_api.delete_namespace.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_gone_namespace_is_not_an_error(
    request_obj: SimpleNamespace, k8s: MagicMock,
) -> None:
    # Racing deletes: the ns vanished between the existence check and the
    # delete call. Teardown is idempotent, so that's a success.
    k8s.core_api.delete_namespace = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found"),
    )

    await tenants_crud.delete_tenant(request_obj, "demo", user=_admin_user())


@pytest.mark.asyncio
async def test_a_described_tenant_is_handed_to_its_finalizer_first(
    request_obj: SimpleNamespace, k8s: MagicMock,
) -> None:
    """The operator's finalizer gives back what outlives a namespace — the
    transit EIP and its ACLs, the demux ports. Deleting the namespace while
    that is in flight is how a teardown wedges, so the description goes first
    and the sweep below waits for it to be gone."""
    gone: list[bool] = [False]

    async def _delete_cr(**_kwargs: object) -> None:
        gone[0] = True

    async def _get_cr(**_kwargs: object) -> dict:
        if gone[0]:
            raise ApiException(status=404, reason="Not Found")
        return {"metadata": {"name": "demo"}}

    k8s.custom_api.delete_cluster_custom_object = AsyncMock(side_effect=_delete_cr)
    k8s.custom_api.get_cluster_custom_object = AsyncMock(side_effect=_get_cr)

    await tenants_crud.delete_tenant(request_obj, "demo", user=_admin_user())

    assert len(_managed_tenant_deletes(k8s)) == 1
    # And the sweep still runs: it removes the same things and finds nothing,
    # which is cheaper than two teardown paths kept in agreement by hand.
    k8s.core_api.delete_namespace.assert_awaited_once_with(name="tenant-demo")


@pytest.mark.asyncio
async def test_a_described_tenant_is_deletable_with_no_namespace_left(
    request_obj: SimpleNamespace, k8s: MagicMock,
) -> None:
    """Ownership is the object. A tenant whose namespace the operator has
    already removed must not answer 404 to the button that removes it."""
    k8s.core_api.read_namespace = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found"),
    )
    k8s.custom_api.delete_cluster_custom_object = AsyncMock()
    k8s.custom_api.get_cluster_custom_object = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found"),
    )

    await tenants_crud.delete_tenant(request_obj, "demo", user=_admin_user())

    assert len(_managed_tenant_deletes(k8s)) == 1
