"""A session is not authorisation.

Every object endpoint under `/tenants/{name}` carried only `require_auth`, so
any logged-in account could read another tenant's **admin** kubeconfig, scale
it, or delete it. The collection endpoints were correct — `GET /tenants`
filters, `POST /tenants` checks folder admin — so the check was lost exactly
at the collection → object boundary, which is also where it is hardest to
notice.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1.tenants_common import require_tenant_access
from app.core.auth import User

FOLDER = {
    "access": {
        "admins": ["team-a-admins"],
        "members": ["team-a-devs"],
        "viewers": ["team-a-readers"],
        # env overrides live inside the access block, not beside it
        "env_access": {"prod": {"admins": ["team-a-prod-admins"]}},
    },
}


def _user(*groups: str) -> User:
    return User(id="u", email="u@local", username="u", groups=list(groups))


def _k8s(folder: str = "team-a", environment: str = "prod") -> MagicMock:
    k8s = MagicMock()
    ns = MagicMock()
    ns.metadata.labels = {
        "kubevirt-ui.io/folder": folder,
        "kubevirt-ui.io/environment": environment,
    }
    k8s.core_api.read_namespace = AsyncMock(return_value=ns)
    return k8s


@pytest.fixture(autouse=True)
def _folder(monkeypatch):
    async def load_folder(_k8s, name):
        return FOLDER

    monkeypatch.setattr("app.core.groups.load_folder", load_folder)


class TestOutsiderIsRefused:
    @pytest.mark.asyncio
    async def test_a_stranger_cannot_read_the_admin_kubeconfig(self) -> None:
        # The C2 case: logged in, no role anywhere near this folder.
        with pytest.raises(HTTPException) as exc:
            await require_tenant_access(_k8s(), _user("some-other-team"), "t1", level="admin")

        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_stranger_cannot_even_read_it(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await require_tenant_access(_k8s(), _user("nobody"), "t1", level="viewer")

        assert exc.value.status_code == 403


class TestRolesAreHonoured:
    @pytest.mark.asyncio
    async def test_a_viewer_may_read_but_not_change(self) -> None:
        user = _user("team-a-readers")
        await require_tenant_access(_k8s(), user, "t1", level="viewer")

        with pytest.raises(HTTPException) as exc:
            await require_tenant_access(_k8s(), user, "t1", level="member")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_member_may_change_but_not_delete(self) -> None:
        user = _user("team-a-devs")
        await require_tenant_access(_k8s(), user, "t1", level="member")

        with pytest.raises(HTTPException) as exc:
            await require_tenant_access(_k8s(), user, "t1", level="admin")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_folder_admin_may_delete(self) -> None:
        await require_tenant_access(_k8s(), _user("team-a-admins"), "t1", level="admin")

    @pytest.mark.asyncio
    async def test_an_env_admin_counts_for_that_environment(self) -> None:
        await require_tenant_access(
            _k8s(environment="prod"), _user("team-a-prod-admins"), "t1", level="admin",
        )

    @pytest.mark.asyncio
    async def test_a_platform_admin_skips_the_lookup_entirely(self) -> None:
        k8s = MagicMock()
        k8s.core_api.read_namespace = AsyncMock(side_effect=AssertionError("not needed"))

        await require_tenant_access(k8s, _user("kubevirt-ui-admins"), "t1", level="admin")


class TestItFailsClosed:
    @pytest.mark.asyncio
    async def test_a_tenant_without_a_folder_is_refused(self) -> None:
        # Falling open here is how this class of hole appears in the first place.
        with pytest.raises(HTTPException) as exc:
            await require_tenant_access(_k8s(folder=""), _user("team-a-admins"), "t1")

        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_the_dangerous_routes_all_call_it(self) -> None:
        import inspect

        from app.api.v1 import tenants_crud

        source = inspect.getsource(tenants_crud)
        for level, marker in [
            ("admin", 'require_tenant_access(k8s, user, name, level="admin")'),
            ("member", 'require_tenant_access(k8s, user, name, level="member")'),
            ("viewer", 'require_tenant_access(k8s, user, name, level="viewer")'),
        ]:
            assert marker in source, f"no {level} check present"

        # kubeconfig and delete are the two that hand over the cluster.
        assert source.count('level="admin"') >= 2
