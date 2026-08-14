"""The sessions table stated a prefix count nobody measured.

On the lab cluster, with `acme-net-default` announced and the neighbour's
table holding five routes:

    $ vtysh -c "show bgp ipv4 unicast"
    *= 10.100.0.0/24  10.198.160.6  0 65001 i     (+ four /32s)
    Displayed 5 routes and 10 total paths

the UI read `Established … Prefixes 0`. `prefixes_received` was hardcoded to
0 and `uptime` to "" — the speaker exposes no BGP metrics (its /metrics has
only Go runtime and klog counters) and its GoBGP API port is disabled, so
neither could ever be filled. A zero next to Established says the
announcement is not working, which is the one conclusion the page must not
invite when it is.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC = Path("app/api/v1/bgp.py").read_text()


def test_nothing_is_reported_as_measured_when_it_is_not() -> None:
    assert "prefixes_received=0" not in SRC
    assert 'uptime=""' not in SRC


def test_the_model_no_longer_carries_the_unfillable_fields() -> None:
    model = Path("app/models/bgp.py").read_text()
    assert "prefixes_received" not in model
    assert "\n    uptime" not in model


@pytest.mark.asyncio
async def test_the_session_reports_how_many_prefixes_are_announced(monkeypatch):
    from app.api.v1 import bgp as mod

    pod = MagicMock()
    pod.status.phase = "Running"
    pod.spec.node_name = "kubevirt-lab-worker-1"
    pod.metadata.name = "kube-ovn-speaker-x"

    k8s = MagicMock()
    k8s.core_api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[pod]))
    k8s.core_api.read_namespaced_pod_log = AsyncMock(
        return_value="INFO Peer Up Key=10.198.160.5",
    )
    ds = MagicMock()
    container = MagicMock()
    container.args = ["--neighbor-address=10.198.160.5", "--neighbor-as=65000"]
    ds.spec.template.spec.containers = [container]
    k8s.apps_api.read_namespaced_daemon_set = AsyncMock(return_value=ds)

    request = MagicMock()
    request.app.state.k8s_client = k8s

    monkeypatch.setattr(mod, "_find_kubeovn_namespace", AsyncMock(return_value="o0-kube-ovn"))
    monkeypatch.setattr(mod, "list_announcements", AsyncMock(return_value=[
        MagicMock(), MagicMock(), MagicMock(),
    ]))

    sessions = await mod.list_sessions(request, user=MagicMock())

    assert len(sessions) == 1
    assert sessions[0].state == "Established"
    assert sessions[0].announced == 3


@pytest.mark.asyncio
async def test_a_broken_announcement_read_does_not_break_the_session_list(monkeypatch):
    """The session state is the more important of the two; losing the count
    must not lose it."""
    from app.api.v1 import bgp as mod

    pod = MagicMock()
    pod.status.phase = "Running"
    pod.spec.node_name = "n1"
    pod.metadata.name = "kube-ovn-speaker-x"

    k8s = MagicMock()
    k8s.core_api.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=[pod]))
    k8s.core_api.read_namespaced_pod_log = AsyncMock(return_value="Peer Up 10.198.160.5")
    ds = MagicMock()
    container = MagicMock()
    container.args = ["--neighbor-address=10.198.160.5", "--neighbor-as=65000"]
    ds.spec.template.spec.containers = [container]
    k8s.apps_api.read_namespaced_daemon_set = AsyncMock(return_value=ds)

    request = MagicMock()
    request.app.state.k8s_client = k8s
    monkeypatch.setattr(mod, "_find_kubeovn_namespace", AsyncMock(return_value="o0-kube-ovn"))
    monkeypatch.setattr(mod, "list_announcements", AsyncMock(side_effect=RuntimeError("boom")))

    sessions = await mod.list_sessions(request, user=MagicMock())

    assert sessions[0].state == "Established"
    assert sessions[0].announced == 0
