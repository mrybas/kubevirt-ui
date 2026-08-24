"""One golden image per Talos version, not per tenant.

It used to be one per tenant: the same bytes pulled over HTTP again for every
tenant created, and — before T3 — written into by the worker that mounted it.
The version is the only thing that tells two golden images apart, so it is the
name, and the name is what makes the singleton hold. Two tenants created in
the same second both try to create `talos-golden-1-13-8`; etcd admits one.

The cross-namespace clone this enables was measured before any of it was
written: CDI's populator on `csi-clone`, about twenty seconds for a 10Gi
source, no clone pods and no snapshot — and gated on `datavolumes/source`,
which the chart did not grant. The lab could never have shown that: its
backend is `system:masters`, for which the check always passes.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client import ApiException

from app.api.v1.tenants_talos import (
    build_shared_golden_dv,
    ensure_shared_golden_image,
    golden_name,
    golden_namespace,
)


class TestTheNameIsTheVersion:
    def test_two_tenants_of_one_version_want_one_object(self) -> None:
        assert golden_name("1.13.8") == golden_name("v1.13.8")

    def test_two_versions_are_two_objects(self) -> None:
        assert golden_name("1.13.8") != golden_name("1.14.0")

    def test_it_is_a_legal_object_name(self) -> None:
        """Dots are legal in a DNS-1123 subdomain but not in every consumer of
        this string; flattening them keeps it usable as a label value too."""
        import re

        name = golden_name("v1.13.8")

        assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)

    def test_the_version_is_recoverable_from_the_object(self) -> None:
        """A name with the dots flattened cannot be parsed back reliably, so
        the version is also a label — for anything that has to ask what this
        image is."""
        dv = build_shared_golden_dv("1.13.8", "https://x/y.raw.xz", "20Gi", None)

        assert dv["metadata"]["labels"]["kubevirt-ui.io/talos-version"] == "1.13.8"


class TestTheSingletonUnderConcurrency:
    @pytest.mark.asyncio
    async def test_the_loser_of_the_race_starts_no_second_import(self) -> None:
        """`create` is the compare-and-swap. Both callers ask; etcd admits one;
        the other must not fall back to creating anything of its own."""
        created: list[str] = []
        exists = {"yes": False}

        async def create(**kw):
            if exists["yes"]:
                raise ApiException(status=409)
            exists["yes"] = True
            created.append(kw["body"]["metadata"]["name"])
            return {}

        async def get(**kw):
            return {"metadata": {"name": kw["name"]},
                    "status": {"phase": "Succeeded"}}

        k8s = MagicMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock(side_effect=create)
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=get)

        import asyncio

        names = await asyncio.gather(*(
            ensure_shared_golden_image(k8s, "1.13.8", "https://x/y.raw.xz")
            for _ in range(4)
        ))

        assert len(created) == 1, f"{len(created)} imports started"
        assert set(names) == {golden_name("1.13.8")}

    @pytest.mark.asyncio
    async def test_a_second_version_is_not_a_conflict(self) -> None:
        created: list[str] = []

        async def create(**kw):
            name = kw["body"]["metadata"]["name"]
            if name in created:
                raise ApiException(status=409)
            created.append(name)
            return {}

        k8s = MagicMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock(side_effect=create)

        await ensure_shared_golden_image(k8s, "1.13.8", "https://x/a.raw.xz")
        await ensure_shared_golden_image(k8s, "1.14.0", "https://x/b.raw.xz")

        assert created == [golden_name("1.13.8"), golden_name("1.14.0")]

    @pytest.mark.asyncio
    async def test_an_unrelated_api_error_is_not_swallowed(self) -> None:
        """Only 409 means "somebody else has it". A 403 treated the same way
        would present as a tenant whose workers clone from nothing."""
        k8s = MagicMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=403))

        with pytest.raises(ApiException):
            await ensure_shared_golden_image(k8s, "1.13.8", "https://x/y.raw.xz")


class TestAGoldenBeingDeletedIsRefused_NotRaced:
    @pytest.mark.asyncio
    async def test_terminating_produces_an_explanation(self) -> None:
        """Recreating an object whose finalizers are still running gives either
        a create that hangs or an import the deletion then removes. Neither is
        a state to hand a tenant."""
        async def get(**kw):
            return {"metadata": {"name": kw["name"],
                                 "deletionTimestamp": "2026-08-18T10:00:00Z"}}

        k8s = MagicMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=409))
        k8s.custom_api.get_namespaced_custom_object = AsyncMock(side_effect=get)

        with pytest.raises(HTTPException) as e:
            await ensure_shared_golden_image(k8s, "1.13.8", "https://x/y.raw.xz")

        assert e.value.status_code == 409
        assert "being deleted" in e.value.detail
        assert "Retry" in e.value.detail


class TestTheWorkerClonesFromTheSharedCopy:
    def test_the_template_sources_across_the_namespace_boundary(self) -> None:
        from app.api.v1.tenants_capi import _build_worker_data_volume_templates
        from app.models.tenant import TenantCreateRequest

        req = TenantCreateRequest(
            name="t9", display_name="t9", folder="f", environment="e",
            worker_os="talos", kubernetes_version="v1.32.1",
        )

        [tpl] = _build_worker_data_volume_templates(req)
        src = tpl["spec"]["source"]["pvc"]

        assert src["namespace"] == golden_namespace()
        assert src["namespace"] != "tenant-t9"
        assert src["name"].startswith("talos-golden-")

    def test_the_source_is_the_version_the_tenant_resolved_to(self) -> None:
        """Cloning from a different version's image would give the tenant a
        kubelet that does not match its control plane."""
        from app.api.v1.tenants_capi import _build_worker_data_volume_templates
        from app.api.v1.tenants_talos import resolve_talos_release
        from app.models.tenant import TenantCreateRequest

        req = TenantCreateRequest(
            name="t9", display_name="t9", folder="f", environment="e",
            worker_os="talos", kubernetes_version="v1.32.1",
        )
        release = resolve_talos_release(req.kubernetes_version, req.talos_version)

        [tpl] = _build_worker_data_volume_templates(req)

        assert tpl["spec"]["source"]["pvc"]["name"] == golden_name(release.talos)


class TestTheQuotaFollowedTheImageOut:
    def test_the_golden_is_no_longer_charged_to_the_tenant(self) -> None:
        """a96df69 added it because the image was imported into the tenant
        namespace. It is not there any more, and a quota that reserves 20Gi for
        something absent is the same defect with the sign reversed."""
        from app.api.v1.tenants_crud import _tenant_quota
        from app.models.tenant import TenantCreateRequest

        req = TenantCreateRequest(
            name="t9", display_name="t9", folder="f", environment="e",
            worker_os="talos", worker_count=2, worker_disk="20Gi",
        )

        # the roots, and only those: surge x 20Gi
        assert int(_tenant_quota(req)["storage"]) == 3 * 20 * 2**30

    def test_the_root_clones_are_still_counted(self) -> None:
        """They do live in the tenant namespace, one per worker."""
        from app.api.v1.tenants_crud import _tenant_quota
        from app.models.tenant import TenantCreateRequest

        def storage(count: int) -> int:
            return int(_tenant_quota(TenantCreateRequest(
                name="t9", display_name="t9", folder="f", environment="e",
                worker_os="talos", worker_count=count, worker_disk="20Gi",
            ))["storage"])

        assert storage(2) - storage(1) == 20 * 2**30


class TestWhoActuallyPerformsTheClone:
    """Two subjects on this path, and they are different.

    Granting the backend `datavolumes/source` was necessary and not
    sufficient. The worker's root disk is a `dataVolumeTemplate` on the
    VirtualMachine, so KubeVirt creates it — and CDI evaluates the tenant
    namespace's default ServiceAccount. The cluster said so in as many words:

        not authorized to create DataVolume: User
        system:serviceaccount:tenant-ga:default has insufficient permissions
        in clone source namespace kubevirt-ui-system

    Unlike the backend's half, the lab *does* catch this one — the actor is a
    tenant ServiceAccount, not the admin kubeconfig. Both halves were needed
    and only one was visible here, which is worth remembering the next time a
    permission looks handled.
    """

    def test_the_role_lives_where_the_golden_is(self) -> None:
        from app.api.v1.tenants_talos import build_golden_cloner_role

        role = build_golden_cloner_role()

        assert role["metadata"]["namespace"] == golden_namespace()
        assert role["rules"][0]["resources"] == ["datavolumes/source"]
        assert role["rules"][0]["verbs"] == ["create"]

    def test_the_subject_is_the_tenant_default_service_account(self) -> None:
        """Not the backend's SA — that one never touches this DataVolume."""
        from app.api.v1.tenants_talos import build_golden_cloner_binding

        rb = build_golden_cloner_binding("tenant-ga")
        [subject] = rb["subjects"]

        assert subject["kind"] == "ServiceAccount"
        assert subject["namespace"] == "tenant-ga"
        assert subject["name"] == "default"

    def test_the_binding_is_per_tenant_and_named_so(self) -> None:
        """One binding per tenant, so removing a tenant does not take another
        tenant's permission with it."""
        from app.api.v1.tenants_talos import build_golden_cloner_binding

        a = build_golden_cloner_binding("tenant-a")["metadata"]["name"]
        b = build_golden_cloner_binding("tenant-b")["metadata"]["name"]

        assert a != b
        assert a.endswith("tenant-a")

    def test_it_grants_nothing_beyond_the_clone(self) -> None:
        """`datavolumes/source` is a permission to be cloned *from*; it gives
        no read of the object and no access to anything else in that
        namespace."""
        from app.api.v1.tenants_talos import build_golden_cloner_role

        [rule] = build_golden_cloner_role()["rules"]

        assert rule["verbs"] == ["create"]
        assert rule["resources"] == ["datavolumes/source"]

    @pytest.mark.asyncio
    async def test_creation_grants_it_before_the_import(self) -> None:
        """Order matters only for tidiness here, but the call has to exist:
        without it every Talos worker stops at FailedCreate and the tenant
        reads as 0/N joined with nothing naming RBAC."""
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()

        assert "ensure_golden_clone_rbac(k8s, ns)" in src
        assert src.index("ensure_golden_clone_rbac") < src.index(
            "ensure_shared_golden_image(")
