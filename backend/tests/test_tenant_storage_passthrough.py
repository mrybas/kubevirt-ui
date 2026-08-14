"""Ticking the CSI addon in the wizard turned tenant storage off.

Created `tstor2` with persistent storage enabled AND the "KubeVirt CSI
Driver" box ticked — the box the same wizard offers, whose two fields (Host
tenant namespace, Host StorageClass) it renders blank. On the cluster:

    HelmRelease tstor2-kubevirt-csi-driver .spec.values
      {"infraClusterNamespace": "", "storageClasses": [...]}

    $ kubectl get pvc pass1
    pass1   Pending
    ProvisioningFailed  rpc error: code = Unknown desc = an empty namespace
                        may not be set when a resource name is provided

`enable_storage` auto-wired the addon only `if KUBEVIRT_CSI_ADDON_ID not in
addon_ids`, so asking for the driver explicitly is what skipped the wiring.
Untick it and the same tenant works: the one path an operator is most likely
to take was the broken one.
"""

from pathlib import Path

import pytest

CRUD = Path("app/api/v1/tenants_crud.py").read_text()


def test_the_wiring_is_not_skipped_when_the_addon_was_asked_for() -> None:
    assert "if req.enable_storage and KUBEVIRT_CSI_ADDON_ID not in addon_ids:" not in CRUD
    assert "if req.enable_storage:" in CRUD


def test_a_ticked_addon_gets_the_missing_parameters_filled_in() -> None:
    block = CRUD[CRUD.index("if req.enable_storage:"):]
    block = block[:block.index("if all_addons and catalog.git_repository_ref")]
    assert "existing.parameters = params" in block
    assert "if not params.get(key)" in block, "anything the operator typed must win"


class TestValues:
    """`_build_helm_values` is the last line before the chart."""

    def _component(self):
        from app.models.tenant import AddonComponent

        return AddonComponent(
            id="kubevirt-csi-driver",
            name="KubeVirt CSI Driver",
            chartPath="kubevirt-csi-driver",
            defaultValues={
                "infraClusterNamespace": "",
                "storageClasses": [{"name": "kubevirt", "infraStorageClassName": ""}],
            },
        )

    def test_an_empty_namespace_is_replaced_with_the_tenants_own(self) -> None:
        from app.api.v1.tenants_addons import _build_helm_values

        values = _build_helm_values("tstor2", self._component(), {})

        assert values["infraClusterNamespace"] == "tenant-tstor2", \
            "an empty namespace is not a default, it is a driver that cannot provision"

    def test_an_explicit_namespace_is_kept(self) -> None:
        from app.api.v1.tenants_addons import _build_helm_values

        values = _build_helm_values(
            "tstor2", self._component(),
            {"INFRA_CLUSTER_NAMESPACE": "somewhere-else"},
        )

        assert values["infraClusterNamespace"] == "somewhere-else"


@pytest.mark.parametrize("params", [{}, {"INFRA_CLUSTER_NAMESPACE": ""}])
def test_the_chart_never_receives_a_blank_namespace(params) -> None:
    from app.api.v1.tenants_addons import _build_helm_values
    from app.models.tenant import AddonComponent

    component = AddonComponent(
        id="kubevirt-csi-driver",
        name="KubeVirt CSI Driver",
        chartPath="kubevirt-csi-driver",
        defaultValues={"infraClusterNamespace": ""},
    )

    values = _build_helm_values("t1", component, params)

    assert values["infraClusterNamespace"]
