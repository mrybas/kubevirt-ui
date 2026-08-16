"""Unit tests for the VPC underlay fabric.

A wizard-created VPC has no way out until a ProviderNetwork, a Vlan and an
external Subnet reachable through a NAD exist. The UI used to look for that
fabric and never build it, so it had to be applied by hand.

The shapes below are the ones verified on the lab; several fields are
load-bearing in ways that fail silently if they drift.
"""

from unittest.mock import AsyncMock, MagicMock

import json
import re

import pytest
from kubernetes_asyncio.client import ApiException
from pydantic import ValidationError

from app.api.v1.vpc_underlay import (
    CILIUM_EXEMPT_NAME,
    EXTERNAL_GW_LABEL,
    LINK_WATCHER_NAME,
    WORKAROUND_LABEL,
    WORKAROUND_REASON,
    WORKAROUND_REMOVE_WHEN,
    VpcUnderlayRequest,
    build_cilium_exempt,
    build_external_nad,
    build_external_subnet,
    build_link_watcher,
    build_provider_network,
    build_vlan,
    _daemonset_state,
    ensure_vpc_underlay,
    get_vpc_underlay,
    subnet_provider,
)

KUBEOVN_NS = "o0-kube-ovn"


def _req(**kw: object) -> VpcUnderlayRequest:
    base: dict = {
        "interface": "eth1",
        "external_cidr": "10.198.176.0/20",
        "external_gateway": "10.198.191.254",
    }
    base.update(kw)
    return VpcUnderlayRequest(**base)  # type: ignore[arg-type]


class TestProviderNetwork:
    def test_uses_the_dedicated_interface(self) -> None:
        assert build_provider_network(_req())["spec"]["defaultInterface"] == "eth1"

    def test_excludes_nodes_without_the_nic(self) -> None:
        # Control planes usually have one NIC; kube-ovn would enslave it.
        pn = build_provider_network(_req(exclude_nodes=["cp-1", "cp-2"]))
        assert pn["spec"]["excludeNodes"] == ["cp-1", "cp-2"]

    def test_no_exclude_key_when_every_node_qualifies(self) -> None:
        assert "excludeNodes" not in build_provider_network(_req())["spec"]


class TestVlan:
    def test_binds_the_vlan_to_the_provider_network(self) -> None:
        vlan = build_vlan(_req(provider_network_name="external"))
        assert vlan["spec"]["provider"] == "external"

    def test_untagged_is_expressible(self) -> None:
        # id 0 = untagged, which is what works under an overlay that eats tags.
        assert build_vlan(_req(vlan_id=0))["spec"]["id"] == 0

    def test_rejects_an_out_of_range_id(self) -> None:
        with pytest.raises(ValidationError):
            _req(vlan_id=5000)


class TestProviderString:
    def test_is_nad_dot_namespace_dot_ovn(self) -> None:
        assert subnet_provider(_req(subnet_name="ext-sub"), KUBEOVN_NS) == (
            f"ext-sub.{KUBEOVN_NS}.ovn"
        )

    def test_subnet_and_nad_agree_character_for_character(self) -> None:
        # kube-ovn compares these exactly; a mismatch makes it refuse the
        # egress gateway with "please set correct provider of subnet ...".
        data = _req()
        subnet = build_external_subnet(data, KUBEOVN_NS)
        nad_config = json.loads(build_external_nad(data, KUBEOVN_NS)["spec"]["config"])
        assert subnet["spec"]["provider"] == nad_config["provider"]


class TestExternalSubnet:
    def test_carries_the_infrastructure_label_the_ui_looks_for(self) -> None:
        # network._find_infra_subnet selects on this.
        labels = build_external_subnet(_req(), KUBEOVN_NS)["metadata"]["labels"]
        assert labels["kubevirt-ui.io/purpose"] == "infrastructure"

    def test_does_not_nat_outgoing(self) -> None:
        # The gateways handle SNAT (or deliberately do not); the underlay must
        # not do it underneath them.
        assert build_external_subnet(_req(), KUBEOVN_NS)["spec"]["natOutgoing"] is False

    def test_gateway_check_disabled(self) -> None:
        # The upstream gateway need not answer kube-ovn's ping, and a failed
        # check blocks the subnet.
        assert build_external_subnet(_req(), KUBEOVN_NS)["spec"]["disableGatewayCheck"] is True

    def test_exclude_ranges_are_passed_through(self) -> None:
        spec = build_external_subnet(
            _req(exclude_ips=["10.198.176.1..10.198.190.199"]), KUBEOVN_NS,
        )["spec"]
        assert spec["excludeIps"] == ["10.198.176.1..10.198.190.199"]

    def test_references_the_vlan(self) -> None:
        spec = build_external_subnet(_req(vlan_name="vlan-external"), KUBEOVN_NS)["spec"]
        assert spec["vlan"] == "vlan-external"


class TestNad:
    def test_lives_where_the_gateways_run(self) -> None:
        nad = build_external_nad(_req(), KUBEOVN_NS)
        assert nad["metadata"]["namespace"] == KUBEOVN_NS

    def test_config_is_kube_ovn_type(self) -> None:
        config = json.loads(build_external_nad(_req(), KUBEOVN_NS)["spec"]["config"])
        assert config["type"] == "kube-ovn"
        assert config["server_socket"].endswith("kube-ovn-daemon.sock")


class TestWorkaroundsAreLabelled:
    def test_link_watcher_says_what_retires_it(self) -> None:
        meta = build_link_watcher(_req(), KUBEOVN_NS)["metadata"]
        assert WORKAROUND_LABEL in meta["labels"]
        assert "kube-ovn" in meta["annotations"][WORKAROUND_REASON]
        assert meta["annotations"][WORKAROUND_REMOVE_WHEN]

    def test_cilium_exempt_says_what_retires_it(self) -> None:
        meta = build_cilium_exempt(_req())["metadata"]
        assert WORKAROUND_LABEL in meta["labels"]
        assert meta["annotations"][WORKAROUND_REASON]
        assert meta["annotations"][WORKAROUND_REMOVE_WHEN]

    def test_link_watcher_targets_the_configured_interface(self) -> None:
        args = build_link_watcher(_req(interface="eno2"), KUBEOVN_NS)[
            "spec"]["template"]["spec"]["containers"][0]["args"][0]
        # The script iterates now — one DaemonSet has to keep every provider
        # NIC up, not only the one this build is for.
        assert "for i in eno2;" in args
        assert 'ip link set dev "$i" up' in args

    def test_link_watcher_only_runs_on_gateway_nodes(self) -> None:
        spec = build_link_watcher(_req(), KUBEOVN_NS)["spec"]["template"]["spec"]
        assert spec["nodeSelector"] == {"ovn.kubernetes.io/external-gw": "true"}

    def test_cilium_exempt_is_per_endpoint_not_global(self) -> None:
        # The global switch would disable anti-spoofing for every pod; tenant
        # worker VMs are root-accessible to their tenants.
        args = build_cilium_exempt(_req())[
            "spec"]["template"]["spec"]["containers"][0]["args"][0]
        assert "SourceIPVerification=disable" in args
        assert "enable-source-ip-verification" not in args


@pytest.mark.asyncio
class TestEnsureEndpoint:
    def _k8s(self, ready_nodes: list[str] | None = None) -> MagicMock:
        k8s = MagicMock()
        k8s.custom_api.create_cluster_custom_object = AsyncMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock()
        # The link watcher asks which provider NICs exist so one DaemonSet can
        # keep all of them up.
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
        k8s.apps_api.create_namespaced_daemon_set = AsyncMock()
        # The ProviderNetwork is read back for the node list.
        nodes = ["worker-1", "worker-2"] if ready_nodes is None else ready_nodes
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            return_value={"status": {"readyNodes": nodes}},
        )
        node = MagicMock()
        node.metadata.labels = {}
        k8s.core_api.read_node = AsyncMock(return_value=node)
        k8s.core_api.patch_node = AsyncMock()
        cni = MagicMock()
        cni.spec.template.spec.containers = [MagicMock(image="kubeovn/kube-ovn:v1.16.0")]
        k8s.apps_api.read_namespaced_daemon_set = AsyncMock(return_value=cni)
        return k8s

    def _request(self, k8s: MagicMock) -> MagicMock:
        request = MagicMock()
        request.app.state.k8s_client = k8s
        return request

    @pytest.fixture(autouse=True)
    def kubeovn_ns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _ns(_k8s: object) -> str:
            return KUBEOVN_NS

        import app.api.v1.network as network_mod
        monkeypatch.setattr(network_mod, "_find_kubeovn_namespace", _ns)

    async def test_builds_the_whole_fabric(self) -> None:
        k8s = self._k8s()

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        kinds = {o.kind for o in result.objects if not o.workaround}
        assert kinds == {
            "ProviderNetwork", "Vlan", "NetworkAttachmentDefinition", "Subnet",
            "NodeLabel",
        }
        assert result.ready is True

    async def test_is_idempotent(self) -> None:
        # Everything already there — a re-run must report success, not 409.
        k8s = self._k8s()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
        k8s.custom_api.create_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists"),
        )
        k8s.custom_api.create_namespaced_custom_object = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists"),
        )
        k8s.apps_api.create_namespaced_daemon_set = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists"),
        )
        k8s.apps_api.patch_namespaced_daemon_set = AsyncMock()
        already = MagicMock()
        already.metadata.labels = {EXTERNAL_GW_LABEL: "true"}
        k8s.core_api.read_node = AsyncMock(return_value=already)

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        assert result.ready is True
        assert all(o.state in ("exists", "skipped") for o in result.objects)
        k8s.core_api.patch_node.assert_not_awaited()

    async def test_existing_daemonset_is_reconciled_not_recreated(self) -> None:
        k8s = self._k8s()
        k8s.apps_api.create_namespaced_daemon_set = AsyncMock(
            side_effect=ApiException(status=409, reason="AlreadyExists"),
        )
        k8s.apps_api.patch_namespaced_daemon_set = AsyncMock()

        await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        k8s.apps_api.patch_namespaced_daemon_set.assert_awaited_once()

    async def test_one_failure_does_not_abort_the_rest(self) -> None:
        # A half-built fabric is easier to finish than to diagnose from a
        # single error, so every object is attempted and reported.
        k8s = self._k8s()
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
        k8s.custom_api.create_cluster_custom_object = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden"),
        )

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        assert result.ready is False
        assert len(result.objects) == 7  # nothing skipped silently

    async def test_workarounds_are_opt_out(self) -> None:
        k8s = self._k8s()

        result = await ensure_vpc_underlay(
            request=self._request(k8s),
            data=_req(link_watcher=False), user=MagicMock(),
        )

        watcher = next(o for o in result.objects if o.name == LINK_WATCHER_NAME)
        assert watcher.state == "skipped"
        assert result.ready is True

    async def test_cilium_exemption_is_off_by_default(self) -> None:
        # A no-op cost on clusters that do not chain Cilium.
        k8s = self._k8s()

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        exempt = next(o for o in result.objects if o.name == CILIUM_EXEMPT_NAME)
        assert exempt.state == "skipped"


class TestEveryLabelIsAcceptableToTheApiServer:
    """Labels are validated by the API server, and these were not.

    The workaround marker carried its own explanation as a label *value* — a
    sentence with spaces. Every object here was built from a pure function and
    unit-tested, and the four fabric objects really were created, so the only
    signal was two DaemonSets coming back 422 and an underlay that reported
    itself built with no link watcher behind it.

    Checked across every builder rather than at the one call site that broke,
    because the next sentence-in-a-label will not be in `_workaround_meta`.
    """

    # From the API server's own message, verbatim.
    VALUE_RE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
    NAME_RE = re.compile(
        r"^([A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?/)?"
        r"([A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)$"
    )

    def _all_label_maps(self, body: dict) -> list[tuple[str, dict]]:
        """Object labels plus the pod template's, which are validated too."""
        maps = [("metadata.labels", body["metadata"].get("labels", {}))]
        template = (
            body.get("spec", {})
            .get("template", {})
            .get("metadata", {})
            .get("labels")
        )
        if template is not None:
            maps.append(("spec.template.metadata.labels", template))
        selector = body.get("spec", {}).get("selector", {}).get("matchLabels")
        if selector is not None:
            maps.append(("spec.selector.matchLabels", selector))
        return maps

    @pytest.mark.parametrize("build", [
        lambda: build_provider_network(_req()),
        lambda: build_vlan(_req()),
        lambda: build_external_nad(_req(), KUBEOVN_NS),
        lambda: build_external_subnet(_req(), KUBEOVN_NS),
        lambda: build_link_watcher(_req(), KUBEOVN_NS),
        lambda: build_cilium_exempt(_req()),
    ])
    def test_labels_would_be_accepted(self, build) -> None:
        body = build()
        for where, labels in self._all_label_maps(body):
            for key, value in labels.items():
                assert self.NAME_RE.match(key), (
                    f"{body['kind']} {where}: key {key!r} is not a valid label key"
                )
                assert len(value) <= 63, (
                    f"{body['kind']} {where}[{key}]: {len(value)} chars, "
                    f"the limit is 63 — {value!r}"
                )
                assert self.VALUE_RE.match(value), (
                    f"{body['kind']} {where}[{key}]: {value!r} is not a valid "
                    "label value; prose belongs in an annotation"
                )

    def test_the_reason_is_not_lost_in_the_move(self) -> None:
        """Moving it to an annotation must not mean dropping it."""
        for meta in (
            build_link_watcher(_req(), KUBEOVN_NS)["metadata"],
            build_cilium_exempt(_req())["metadata"],
        ):
            reason = meta["annotations"][WORKAROUND_REASON]
            assert " " in reason, "the reason should still be a sentence"
            assert len(reason) > 20


@pytest.mark.asyncio
class TestGatewayNodesAreLabelled:
    """The link watcher selects on a label nothing was setting.

    `provider-link-up` carries `nodeSelector: ovn.kubernetes.io/external-gw`,
    and building the fabric through the endpoint never applied that label. The
    DaemonSet is created, matches no node, and sits at 0/0 desired: the fabric
    reports itself built, the provider NIC drops back DOWN with nobody
    watching, and every frame is swallowed with OVS still listing the port.

    Measured on the lab: after a successful build,
    `kubectl get nodes -l ovn.kubernetes.io/external-gw=true` returned
    "No resources found".
    """

    def _k8s(self, ready_nodes: list[str], labels: dict | None = None) -> MagicMock:
        k8s = MagicMock()
        k8s.custom_api.create_cluster_custom_object = AsyncMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock()
        # The link watcher asks which provider NICs exist so one DaemonSet can
        # keep all of them up.
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
        k8s.apps_api.create_namespaced_daemon_set = AsyncMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            return_value={"status": {"readyNodes": ready_nodes}},
        )
        node = MagicMock()
        node.metadata.labels = labels or {}
        k8s.core_api.read_node = AsyncMock(return_value=node)
        k8s.core_api.patch_node = AsyncMock()
        cni = MagicMock()
        cni.spec.template.spec.containers = [MagicMock(image="kubeovn/kube-ovn:v1.16.0")]
        k8s.apps_api.read_namespaced_daemon_set = AsyncMock(return_value=cni)
        return k8s

    def _request(self, k8s: MagicMock) -> MagicMock:
        request = MagicMock()
        request.app.state.k8s_client = k8s
        return request

    @pytest.fixture(autouse=True)
    def _no_waiting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.api.v1.vpc_underlay as mod
        import app.api.v1.network as network_mod

        async def _ns(_k8s: object) -> str:
            return KUBEOVN_NS

        monkeypatch.setattr(network_mod, "_find_kubeovn_namespace", _ns)
        monkeypatch.setattr(mod, "_READY_NODE_POLL_SECONDS", 0)
        monkeypatch.setattr(mod, "_READY_NODE_POLL_ATTEMPTS", 2)

    async def test_the_watchers_selector_is_the_label_the_endpoint_sets(self) -> None:
        """The coupling itself, asserted rather than assumed."""
        selector = build_link_watcher(_req(), KUBEOVN_NS)[
            "spec"]["template"]["spec"]["nodeSelector"]
        assert selector == {EXTERNAL_GW_LABEL: "true"}

    async def test_ready_nodes_get_the_label(self) -> None:
        k8s = self._k8s(["worker-1", "worker-2"])

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        patched = {c.kwargs["name"] for c in k8s.core_api.patch_node.await_args_list}
        assert patched == {"worker-1", "worker-2"}
        body = k8s.core_api.patch_node.await_args_list[0].kwargs["body"]
        assert body["metadata"]["labels"][EXTERNAL_GW_LABEL] == "true"
        assert result.ready is True

    async def test_no_ready_nodes_is_a_failure_not_a_quiet_success(self) -> None:
        # The state that shipped: fabric built, nothing labelled, ready=True.
        k8s = self._k8s([])

        result = await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        assert result.ready is False
        label_obj = next(o for o in result.objects if o.kind == "NodeLabel")
        assert label_obj.state == "failed"
        assert "no ready nodes" in label_obj.detail

    async def test_get_reports_an_unlabelled_cluster(self) -> None:
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value={})
        k8s.apps_api.list_daemon_set_for_all_namespaces = AsyncMock(
            return_value=MagicMock(items=[]),
        )
        k8s.core_api.list_node = AsyncMock(return_value=MagicMock(items=[]))

        result = await get_vpc_underlay(request=self._request(k8s), user=MagicMock())

        label_obj = next(o for o in result.objects if o.kind == "NodeLabel")
        assert label_obj.state == "missing"
        assert result.ready is False, (
            "a fabric with no gateway nodes cannot host a gateway"
        )


@pytest.mark.asyncio
class TestTheWatcherCanActuallyStart:
    """Existing is not running, and the difference is the whole point.

    The watcher shipped pinned to `mirror.gcr.io/library/busybox:1.36`. That
    mirror stopped serving one of the image's layers, so on a fresh cluster
    every pod sat in ImagePullBackOff:

        Failed to pull image "mirror.gcr.io/library/busybox:1.36": ...
        could not fetch content descriptor sha256:034d65... not found

    DaemonSet: desired 3, current 3, ready 0. Object created, endpoint green,
    provider link unwatched.
    """

    def _k8s(self, cni_image: str = "kubeovn/kube-ovn:v1.16.0") -> MagicMock:
        k8s = MagicMock()
        k8s.custom_api.create_cluster_custom_object = AsyncMock()
        k8s.custom_api.create_namespaced_custom_object = AsyncMock()
        # The link watcher asks which provider NICs exist so one DaemonSet can
        # keep all of them up.
        k8s.custom_api.list_cluster_custom_object = AsyncMock(return_value={"items": []})
        k8s.custom_api.get_cluster_custom_object = AsyncMock(
            return_value={"status": {"readyNodes": ["worker-1"]}},
        )
        node = MagicMock()
        node.metadata.labels = {}
        k8s.core_api.read_node = AsyncMock(return_value=node)
        k8s.core_api.patch_node = AsyncMock()
        k8s.apps_api.create_namespaced_daemon_set = AsyncMock()
        if cni_image:
            cni = MagicMock()
            cni.spec.template.spec.containers = [MagicMock(image=cni_image)]
            k8s.apps_api.read_namespaced_daemon_set = AsyncMock(return_value=cni)
        else:
            k8s.apps_api.read_namespaced_daemon_set = AsyncMock(
                side_effect=ApiException(status=404, reason="NotFound"),
            )
        return k8s

    def _request(self, k8s: MagicMock) -> MagicMock:
        request = MagicMock()
        request.app.state.k8s_client = k8s
        return request

    @pytest.fixture(autouse=True)
    def _ns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.api.v1.network as network_mod

        async def _f(_k8s: object) -> str:
            return KUBEOVN_NS

        monkeypatch.setattr(network_mod, "_find_kubeovn_namespace", _f)

    async def test_reuses_the_image_already_on_those_nodes(self) -> None:
        k8s = self._k8s(cni_image="kubeovn/kube-ovn:v1.16.0")

        await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        body = k8s.apps_api.create_namespaced_daemon_set.await_args.kwargs["body"]
        image = body["spec"]["template"]["spec"]["containers"][0]["image"]
        assert image == "kubeovn/kube-ovn:v1.16.0"

    async def test_an_explicit_image_still_wins(self) -> None:
        k8s = self._k8s()

        await ensure_vpc_underlay(
            request=self._request(k8s),
            data=_req(link_watcher_image="registry.internal/busybox:1.36"),
            user=MagicMock(),
        )

        body = k8s.apps_api.create_namespaced_daemon_set.await_args.kwargs["body"]
        assert body["spec"]["template"]["spec"]["containers"][0]["image"] == (
            "registry.internal/busybox:1.36"
        )

    async def test_no_default_reaches_a_mirror_that_can_rot(self) -> None:
        # Not a hard rule against public images — a rule against the one that
        # already failed, and against silently shipping an unpinned default.
        k8s = self._k8s(cni_image="")

        await ensure_vpc_underlay(
            request=self._request(k8s), data=_req(), user=MagicMock(),
        )

        body = k8s.apps_api.create_namespaced_daemon_set.await_args.kwargs["body"]
        image = body["spec"]["template"]["spec"]["containers"][0]["image"]
        assert "mirror.gcr.io" not in image
        assert image  # something must be set; an empty image is not a fallback


class TestDaemonSetStateIsAboutPodsNotObjects:
    def _ds(self, desired: int, ready: int) -> MagicMock:
        ds = MagicMock()
        ds.metadata.name = LINK_WATCHER_NAME
        ds.metadata.namespace = KUBEOVN_NS
        ds.status.desired_number_scheduled = desired
        ds.status.number_ready = ready
        return ds

    def test_scheduled_nowhere_is_a_failure(self) -> None:
        obj = _daemonset_state(self._ds(desired=0, ready=0))
        assert obj.state == "failed"
        assert "no node" in obj.detail

    def test_scheduled_but_never_started_is_a_failure(self) -> None:
        obj = _daemonset_state(self._ds(desired=3, ready=0))
        assert obj.state == "failed"
        assert "0/3" in obj.detail

    def test_running_says_how_many(self) -> None:
        obj = _daemonset_state(self._ds(desired=3, ready=3))
        assert obj.state == "exists"
        assert "3/3" in obj.detail


@pytest.mark.asyncio
class TestGetReportsWhatIsRunning:
    """The read path has to use the pod counts, not just find the object.

    Both DaemonSets were also looked up by name in a fixed namespace, and the
    Cilium one lives wherever Cilium does — a namespace this endpoint is never
    told. It was skipped outright, so it never appeared in the status at all,
    working or not.
    """

    def _request(self, k8s: MagicMock) -> MagicMock:
        request = MagicMock()
        request.app.state.k8s_client = k8s
        return request

    @pytest.fixture(autouse=True)
    def _ns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.api.v1.network as network_mod

        async def _f(_k8s: object) -> str:
            return KUBEOVN_NS

        monkeypatch.setattr(network_mod, "_find_kubeovn_namespace", _f)

    def _k8s(self, daemonsets: list) -> MagicMock:
        k8s = MagicMock()
        k8s.custom_api.get_cluster_custom_object = AsyncMock(return_value={})
        node = MagicMock()
        node.metadata.name = "worker-1"
        k8s.core_api.list_node = AsyncMock(return_value=MagicMock(items=[node]))
        k8s.apps_api.list_daemon_set_for_all_namespaces = AsyncMock(
            return_value=MagicMock(items=daemonsets),
        )
        return k8s

    def _ds(self, name: str, ns: str, desired: int, ready: int) -> MagicMock:
        ds = MagicMock()
        ds.metadata.name = name
        ds.metadata.namespace = ns
        ds.status.desired_number_scheduled = desired
        ds.status.number_ready = ready
        return ds

    async def test_a_watcher_that_never_started_is_not_reported_as_present(self) -> None:
        k8s = self._k8s([self._ds(LINK_WATCHER_NAME, KUBEOVN_NS, 3, 0)])

        result = await get_vpc_underlay(request=self._request(k8s), user=MagicMock())

        watcher = next(o for o in result.objects if o.name == LINK_WATCHER_NAME)
        assert watcher.state == "failed"
        assert "0/3" in watcher.detail

    async def test_the_cilium_workaround_is_found_wherever_it_lives(self) -> None:
        k8s = self._k8s([self._ds(CILIUM_EXEMPT_NAME, "o0-cilium", 6, 6)])

        result = await get_vpc_underlay(request=self._request(k8s), user=MagicMock())

        exempt = next(o for o in result.objects if o.name == CILIUM_EXEMPT_NAME)
        assert exempt.namespace == "o0-cilium"
        assert exempt.state == "exists"


class TestLinkWatcherCoversEveryProvider:
    """One DaemonSet, every provider interface.

    The watcher used to hold a single hardcoded `ip link set dev <iface> up`,
    and there is only one DaemonSet per cluster — so building a second underlay
    rewrote its args and the first provider NIC lost its keeper. Measured in
    run #2: after the egress underlay was built, the watcher ran only
    `eth0.310`, and `eth0.300` went down on two of three workers within
    minutes, taking the control-plane transit with it. Nothing looked wrong —
    OVS still showed the port, the subnet stayed Ready (backlog U3).

    In the target design there are always at least two underlays, so this is
    the normal case rather than an edge one.
    """

    def test_every_interface_is_kept_up(self) -> None:
        args = build_link_watcher(
            _req(interface="eth0.310"), KUBEOVN_NS,
            interfaces=["eth0.300", "eth0.310", "eth0.320"],
        )["spec"]["template"]["spec"]["containers"][0]["args"][0]

        for iface in ("eth0.300", "eth0.310", "eth0.320"):
            assert iface in args, f"{iface} lost its keeper"

    def test_the_new_interface_is_included_even_if_not_listed(self) -> None:
        args = build_link_watcher(
            _req(interface="eth0.330"), KUBEOVN_NS, interfaces=["eth0.300"],
        )["spec"]["template"]["spec"]["containers"][0]["args"][0]

        assert "eth0.330" in args
        assert "eth0.300" in args

    def test_duplicates_do_not_multiply_the_loop(self) -> None:
        args = build_link_watcher(
            _req(interface="eth0.300"), KUBEOVN_NS,
            interfaces=["eth0.300", "eth0.300"],
        )["spec"]["template"]["spec"]["containers"][0]["args"][0]

        assert args.count("eth0.300") == 1
