"""Unit tests for the kube-ovn-speaker DaemonSet the UI builds.

The speaker refuses to start without a node name and says so plainly:

    failed to parse config: missing required flags: --node-name must be
    specified (usually via NODE_NAME env from downward API)

The DaemonSet the UI wrote passed only POD_IP, so every speaker pod
crash-looped from birth while the page reported "Speaker Deployed" with
three Running pods and no session ever opening.
"""

from types import SimpleNamespace

from app.api.v1.bgp import _build_speaker_daemonset, _speaker_pod_status


def _daemonset() -> dict:
    return _build_speaker_daemonset(
        namespace="kube-ovn",
        image="docker.io/kubeovn/kube-ovn:v1.16.2",
        neighbor_address="10.198.175.254",
        neighbor_as=65000,
        cluster_as=65001,
        announce_cluster_ip=True,
    )


def _env(ds: dict) -> dict[str, str]:
    container = ds["spec"]["template"]["spec"]["containers"][0]
    return {
        e["name"]: e.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath", "")
        for e in container["env"]
    }


class TestSpeakerDaemonSet:
    def test_node_name_comes_from_the_downward_api(self) -> None:
        assert _env(_daemonset())["NODE_NAME"] == "spec.nodeName"

    def test_pod_identity_is_passed_too(self) -> None:
        env = _env(_daemonset())
        assert env["POD_IP"] == "status.podIP"
        assert env["POD_NAME"] == "metadata.name"
        assert env["POD_NAMESPACE"] == "metadata.namespace"

    def test_peer_settings_reach_the_args(self) -> None:
        args = _daemonset()["spec"]["template"]["spec"]["containers"][0]["args"]
        assert "--neighbor-address=10.198.175.254" in args
        assert "--neighbor-as=65000" in args
        assert "--cluster-as=65001" in args
        assert "--announce-cluster-ip=true" in args

    def test_cluster_ip_announcement_is_opt_in(self) -> None:
        ds = _build_speaker_daemonset(
            namespace="kube-ovn", image="img", neighbor_address="10.0.0.1",
            neighbor_as=65000, cluster_as=65001, announce_cluster_ip=False,
        )
        args = ds["spec"]["template"]["spec"]["containers"][0]["args"]
        assert not any(a.startswith("--announce-cluster-ip") for a in args)


def _pod(*, phase="Running", waiting=None, terminated=None, running=False, ready=False):
    state = SimpleNamespace(
        waiting=SimpleNamespace(reason=waiting) if waiting else None,
        terminated=SimpleNamespace(reason=terminated) if terminated else None,
        running=SimpleNamespace() if running else None,
    )
    return SimpleNamespace(
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[SimpleNamespace(state=state, ready=ready)],
        ),
    )


class TestSpeakerPodStatus:
    def test_a_crash_looping_pod_is_not_reported_as_running(self) -> None:
        # Phase stays "Running" through a crash loop — that is what made a
        # speaker that never started look healthy on the page.
        assert _speaker_pod_status(_pod(waiting="CrashLoopBackOff")) == "CrashLoopBackOff"

    def test_a_terminated_container_reports_its_reason(self) -> None:
        assert _speaker_pod_status(_pod(terminated="Error")) == "Error"

    def test_a_running_but_unready_container_reads_as_starting(self) -> None:
        assert _speaker_pod_status(_pod(running=True, ready=False)) == "Starting"

    def test_a_healthy_pod_still_reads_as_running(self) -> None:
        assert _speaker_pod_status(_pod(running=True, ready=True)) == "Running"

    def test_a_pod_with_no_status_is_unknown(self) -> None:
        assert _speaker_pod_status(SimpleNamespace(status=None)) == "Unknown"
