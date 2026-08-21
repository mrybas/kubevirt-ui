"""Creating a tenant by describing it.

The reason this path exists is one measured failure: a tenant built by the
request handler lost the race with its own transit EIP getting an address. The
handler noticed, logged "ACLs deferred to the next reconcile", and there is no
reconcile — `_wire_tenant_to_transit` has exactly one caller and it is create.
The workers dialled a control plane an ACL was dropping, indefinitely, and every
condition the product shows stayed green.

So the tests here are not about the happy path, which is one create call. They
are about the two ways a handover of this size goes wrong: describing less than
was asked for, and stranding what is already built.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.tenant import TenantCreateRequest


def _req(**over):
    base = dict(
        name="t1", display_name="T1", folder="acme", environment="dev",
        kubernetes_version="v1.33.5", worker_os="talos", vpc_name="net-b",
    )
    base.update(over)
    return TenantCreateRequest(**base)


# --- describing less than was asked for -------------------------------------

def test_every_request_field_is_either_carried_or_refused():
    """The guard the comment beside `_UNDESCRIBABLE_FIELDS` promises.

    A field added to TenantCreateRequest and to neither list is carried
    nowhere and refused nowhere — accepted, described without it, and built
    into a tenant that differs from the one that was asked for with nothing
    anywhere saying so. That is the failure this whole migration keeps
    producing, and it is cheap to make impossible.
    """
    from app.api.v1.tenants_crud import _UNDESCRIBABLE_FIELDS, _managed_tenant_body

    described = _managed_tenant_body(_req(), "ceph-block")
    carried = {
        # request field -> where it lands, named rather than inferred so that
        # renaming one end of the mapping breaks this test rather than the
        # tenant.
        "name", "display_name", "folder", "environment", "kubernetes_version",
        "control_plane_replicas", "worker_count", "worker_vcpu", "worker_memory",
        "worker_disk", "worker_os", "talos_version", "pod_cidr", "service_cidr",
        "enable_oidc", "addons", "vpc_name", "storage_class", "storage_quota_gi",
        "storage_pvc_count",
        # Not a field of the description: it decides whether the host-side CSI
        # resources are written beside it, which stays with the product.
        "enable_storage",
    }
    refused = {name for name, _unset, _why in _UNDESCRIBABLE_FIELDS}
    unaccounted = set(TenantCreateRequest.model_fields) - carried - refused

    assert not unaccounted, (
        f"TenantCreateRequest has fields the operator path neither carries nor "
        f"refuses: {sorted(unaccounted)}. Add each to `_UNDESCRIBABLE_FIELDS` "
        f"or to ManagedTenantSpec — silently dropping one builds the wrong "
        f"tenant."
    )
    assert described["spec"]["displayName"] == "T1"
    assert described["spec"]["network"] == "net-b"


def test_a_worker_image_url_is_refused_rather_than_dropped():
    from app.api.v1.tenants_crud import _undescribable_fields

    named = _undescribable_fields(_req(worker_image_url="http://example/img.qcow2"))

    assert any("worker_image_url" in n for n in named)


def test_dns_and_oidc_groups_are_refused_too():
    from app.api.v1.tenants_crud import _undescribable_fields

    assert _undescribable_fields(_req(dns_servers=["10.0.0.53"]))
    assert _undescribable_fields(_req(admin_group="platform-admins"))
    assert _undescribable_fields(_req(worker_network_binding="masquerade"))


def test_an_ordinary_request_is_describable():
    from app.api.v1.tenants_crud import _undescribable_fields

    assert _undescribable_fields(_req()) == []


# --- not stranding what is already built ------------------------------------

@pytest.mark.asyncio
async def test_delete_removes_the_description_whatever_the_flag_says():
    """Ownership is the object, not the flag.

    A tenant the operator holds has to stay deletable after the flag goes off,
    or turning it off strands every tenant created while it was on.
    """
    from app.api.v1 import tenants_crud

    k8s = AsyncMock()
    with patch.object(tenants_crud, "tenant_path_enabled", return_value=False):
        assert await tenants_crud._delete_managed_tenant(k8s, "t1") is True
    assert k8s.custom_api.delete_cluster_custom_object.await_count == 1


@pytest.mark.asyncio
async def test_a_tenant_the_operator_never_held_reports_so():
    """404 is the answer that lets the product's own teardown run."""
    from kubernetes_asyncio.client import ApiException

    from app.api.v1 import tenants_crud

    k8s = AsyncMock()
    k8s.custom_api.delete_cluster_custom_object.side_effect = ApiException(status=404)

    assert await tenants_crud._delete_managed_tenant(k8s, "t1") is False


@pytest.mark.asyncio
async def test_a_refused_description_is_taken_back():
    """An object nobody will act on is worse than no object: the tenant page
    would list a tenant that is never built."""
    from app.api.v1 import tenants_crud

    k8s = AsyncMock()
    k8s.custom_api.get_cluster_custom_object.return_value = {
        "status": {"conditions": [
            {"type": "Accepted", "status": "False", "reason": "Refused",
             "message": "serviceCIDR overlaps the host"},
        ]},
    }
    with pytest.raises(HTTPException) as caught:
        await tenants_crud._await_managed_tenant(k8s, "t1", timeout=5)

    assert caught.value.status_code == 400
    assert "overlaps the host" in caught.value.detail


@pytest.mark.asyncio
async def test_a_slow_operator_is_not_an_error():
    """The description is written and the operator holds it. Reporting a
    failure here invites a retry of a create that has already happened."""
    from app.api.v1 import tenants_crud

    k8s = AsyncMock()
    k8s.custom_api.get_cluster_custom_object.return_value = {"status": {}}

    await tenants_crud._await_managed_tenant(k8s, "t1", timeout=0)
