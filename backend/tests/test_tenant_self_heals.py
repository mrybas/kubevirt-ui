"""A tenant worker whose node dies has to be replaced.

Killing a worker's VMI on the cluster brought the VM straight back — same
name, fresh container disk, none of the kubelet's configuration on it — and
the tenant kept a node that never returned:

    tci-workers-dhnn8-96ww7   NotReady   (8+ minutes, nothing happened)
    Machine: Ready=True InfrastructureReady=True NodeHealthy=Unknown

CAPI is content because the infrastructure VM exists; only the node is gone.
A MachineHealthCheck is what turns that into a replacement, and no tenant had
one.
"""

from unittest.mock import MagicMock

from app.api.v1.tenants_capi import _build_machine_health_check_cr


def _req():
    req = MagicMock()
    req.name = "tci"
    return req


class TestMachineHealthCheck:
    def test_it_is_created_for_the_worker_deployment(self) -> None:
        cr = _build_machine_health_check_cr(_req())
        assert cr["kind"] == "MachineHealthCheck"
        assert cr["spec"]["clusterName"] == "tci"
        assert cr["spec"]["selector"]["matchLabels"] == {
            "cluster.x-k8s.io/deployment-name": "tci-workers",
        }

    def test_it_remediates_both_false_and_unknown(self) -> None:
        # A node whose kubelet never comes back reports Unknown, not False —
        # which is exactly what the killed worker did.
        conds = {
            (c["type"], c["status"])
            for c in _build_machine_health_check_cr(_req())["spec"]["unhealthyConditions"]
        }
        assert ("Ready", "False") in conds
        assert ("Ready", "Unknown") in conds

    def test_it_will_replace_the_only_worker(self) -> None:
        # The default 40% guard refuses to remediate a one-worker tenant —
        # precisely when the tenant is entirely down.
        assert _build_machine_health_check_cr(_req())["spec"]["maxUnhealthy"] == "100%"

    def test_it_waits_long_enough_for_a_first_boot(self) -> None:
        spec = _build_machine_health_check_cr(_req())["spec"]
        assert spec["nodeStartupTimeout"] == "20m"

    def test_the_creation_path_includes_it(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_capi.py").read_text()
        assert '"machinehealthchecks", _build_machine_health_check_cr(req)' in src


class TestRemediationCanFinish:
    """Deleting a dead worker's Machine drains its node first.

    A node that is gone cannot be drained, and CAPI retries forever unless a
    timeout says otherwise. On the cluster the remediation cordoned the node
    and then stopped:

        DrainingSucceeded=False Draining: cannot evict pod as it would violate
        the pod's disruption budget. The disruption budget calico-typha needs
        0 healthy pods and has 0 currently

    — Machine deleting since 07:35, replacement never created.
    """

    def test_the_worker_template_gives_the_drain_a_deadline(self) -> None:
        from unittest.mock import MagicMock

        from app.api.v1.tenants_capi import _build_machine_deployment_cr

        req = MagicMock()
        req.name = "tci"
        req.kubernetes_version = "v1.32.1"
        req.worker_count = 1
        req.worker_os = "cloud-init"
        cr = _build_machine_deployment_cr(req)
        assert cr["spec"]["template"]["spec"]["nodeDrainTimeout"] == "5m"
