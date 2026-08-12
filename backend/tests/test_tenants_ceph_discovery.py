"""Unit tests for Ceph storage discovery in the tenant wizard path."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.tenants_crud import (
    _ceph_sc_mode,
    _discover_ceph,
    _suggest_ceph_sc,
    suggested_infra_storage_class,
)
from app.models.tenant import StorageClassInfo


def _sc(name: str, provisioner: str, binding: str = "Immediate",
        default: bool = False) -> MagicMock:
    sc = MagicMock()
    sc.metadata.name = name
    sc.metadata.annotations = (
        {"storageclass.kubernetes.io/is-default-class": "true"} if default else {}
    )
    sc.provisioner = provisioner
    sc.volume_binding_mode = binding
    return sc


def _k8s() -> MagicMock:
    k8s = MagicMock()
    k8s._api_client = MagicMock()
    return k8s


@pytest.fixture
def storage_api(monkeypatch: pytest.MonkeyPatch):
    """Stub StorageV1Api so _discover_ceph sees a chosen StorageClass list."""
    holder = MagicMock()

    def _factory(classes: list[MagicMock]) -> None:
        api = MagicMock()
        api.list_storage_class = AsyncMock(return_value=MagicMock(items=classes))
        monkeypatch.setattr(
            "app.api.v1.tenants_crud.StorageV1Api", MagicMock(return_value=api),
        )

    holder.set = _factory
    return holder


class TestProvisionerMatching:
    """The leading component is the Rook operator namespace, not `rook-ceph`."""

    @pytest.mark.parametrize("provisioner,expected", [
        ("rook-ceph.rbd.csi.ceph.com", "block"),
        ("o0-rook-ceph.rbd.csi.ceph.com", "block"),      # non-default install
        ("rook-ceph.cephfs.csi.ceph.com", "filesystem"),
        ("o0-rook-ceph.cephfs.csi.ceph.com", "filesystem"),
        ("linstor.csi.linbit.com", ""),
        ("kubernetes.io/no-provisioner", ""),
        ("", ""),
    ])
    def test_mode_from_provisioner(self, provisioner: str, expected: str) -> None:
        assert _ceph_sc_mode(provisioner) == expected


class TestSuggestion:
    def _info(self, name: str, mode: str, binding: str,
              default: bool = False) -> StorageClassInfo:
        return StorageClassInfo(
            name=name, mode=mode, volume_binding_mode=binding, is_default=default,
        )

    def test_prefers_rbd_over_cephfs(self) -> None:
        picked = _suggest_ceph_sc([
            self._info("ceph-filesystem", "filesystem", "Immediate"),
            self._info("ceph-block", "block", "Immediate"),
        ])
        assert picked is not None and picked.name == "ceph-block"

    def test_prefers_immediate_binding(self) -> None:
        picked = _suggest_ceph_sc([
            self._info("ceph-block-wffc", "block", "WaitForFirstConsumer"),
            self._info("ceph-block", "block", "Immediate"),
        ])
        assert picked is not None and picked.name == "ceph-block"

    def test_rbd_wins_over_a_default_cephfs(self) -> None:
        picked = _suggest_ceph_sc([
            self._info("ceph-filesystem", "filesystem", "Immediate", default=True),
            self._info("ceph-block", "block", "Immediate"),
        ])
        assert picked is not None and picked.name == "ceph-block"

    def test_default_breaks_ties(self) -> None:
        picked = _suggest_ceph_sc([
            self._info("ceph-block-b", "block", "Immediate"),
            self._info("ceph-block-a", "block", "Immediate", default=True),
        ])
        assert picked is not None and picked.name == "ceph-block-a"

    def test_empty_list(self) -> None:
        assert _suggest_ceph_sc([]) is None


@pytest.mark.asyncio
class TestDiscoverCeph:
    async def test_returns_none_without_ceph_classes(self, storage_api) -> None:
        storage_api.set([_sc("linstor-r2", "linstor.csi.linbit.com")])
        assert await _discover_ceph(_k8s()) is None

    async def test_collects_ceph_classes_only(self, storage_api) -> None:
        storage_api.set([
            _sc("linstor-r2", "linstor.csi.linbit.com"),
            _sc("ceph-block", "o0-rook-ceph.rbd.csi.ceph.com"),
            _sc("ceph-filesystem", "o0-rook-ceph.cephfs.csi.ceph.com"),
        ])
        result = await _discover_ceph(_k8s())

        assert result is not None
        assert result.type == "ceph"
        assert result.api_url == ""
        assert [sc.name for sc in result.storage_classes] == [
            "ceph-block", "ceph-filesystem",
        ]

    async def test_marks_exactly_one_suggestion(self, storage_api) -> None:
        storage_api.set([
            _sc("ceph-filesystem", "o0-rook-ceph.cephfs.csi.ceph.com"),
            _sc("ceph-block", "o0-rook-ceph.rbd.csi.ceph.com"),
            _sc("ceph-block-wffc", "o0-rook-ceph.rbd.csi.ceph.com",
                binding="WaitForFirstConsumer"),
        ])
        result = await _discover_ceph(_k8s())

        assert result is not None
        suggested = [sc.name for sc in result.storage_classes if sc.suggested]
        assert suggested == ["ceph-block"]

    async def test_reads_default_annotation(self, storage_api) -> None:
        storage_api.set([
            _sc("ceph-block", "o0-rook-ceph.rbd.csi.ceph.com", default=True),
        ])
        result = await _discover_ceph(_k8s())

        assert result is not None
        assert result.storage_classes[0].is_default is True


@pytest.mark.asyncio
class TestSuggestedInfraStorageClass:
    async def test_returns_the_suggested_name(self, storage_api) -> None:
        storage_api.set([
            _sc("ceph-filesystem", "o0-rook-ceph.cephfs.csi.ceph.com"),
            _sc("ceph-block", "o0-rook-ceph.rbd.csi.ceph.com"),
        ])
        assert await suggested_infra_storage_class(_k8s()) == "ceph-block"

    async def test_empty_when_no_ceph(self, storage_api) -> None:
        # Preserves the old behaviour: fall through to the cluster default.
        storage_api.set([_sc("linstor-r2", "linstor.csi.linbit.com")])
        assert await suggested_infra_storage_class(_k8s()) == ""
