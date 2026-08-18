"""The replacement window is derived from a measurement, not chosen.

A Talos worker rebooted through the UI came back in about three minutes: VMI
recreated, node rejoined, tenant Ready 2/2. The window was five, and the MHC
did fire

    Normal DetectedUnhealthy machine/t3fix-workers-zm2sh-j4tr6

during that reboot — it started its clock and only failed to remediate because
the node beat it by ninety seconds. A larger image, a busier node or a loaded
Ceph crosses that line, and the operation that crosses it is an ordinary
reboot: the tenant loses a worker's identity and re-clones a 20Gi root for no
reason at all.

Three times the observed return, so the number moves when the measurement
does. The cost is real and deliberate: a genuinely dead worker waits about
five minutes longer for its replacement. For persistent nodes a false
replacement is dearer than a slow one.
"""

from app.api.v1.tenants_capi import (
    WORKER_RETURN_TIME_OBSERVED,
    WORKER_UNHEALTHY_TIMEOUT,
    _build_machine_health_check_cr,
)
from app.models.tenant import TenantCreateRequest


def _req(**kw) -> TenantCreateRequest:
    base = dict(name="t9", display_name="t9", folder="f", environment="e")
    base.update(kw)
    return TenantCreateRequest(**base)


class TestTheWindowFollowsTheMeasurement:
    def test_it_is_three_times_the_observed_return(self) -> None:
        assert WORKER_UNHEALTHY_TIMEOUT == f"{WORKER_RETURN_TIME_OBSERVED * 3}m"

    def test_it_is_comfortably_past_the_measured_reboot(self) -> None:
        """Ninety seconds of margin is what made the old value a trap."""
        minutes = int(WORKER_UNHEALTHY_TIMEOUT.removesuffix("m"))

        assert minutes - WORKER_RETURN_TIME_OBSERVED >= 5

    def test_both_unhealthy_conditions_use_it(self) -> None:
        """`Ready=Unknown` is the one a rebooting node actually goes through —
        the kubelet stops reporting rather than reporting False."""
        spec = _build_machine_health_check_cr(_req())["spec"]

        assert {c["timeout"] for c in spec["unhealthyConditions"]} == \
            {WORKER_UNHEALTHY_TIMEOUT}
        assert {c["status"] for c in spec["unhealthyConditions"]} == \
            {"False", "Unknown"}


class TestWhatMustNotMoveWithIt:
    def test_node_startup_timeout_is_left_alone(self) -> None:
        """It governs a node that has never joined at all — a different
        failure, separately verified as sufficient."""
        spec = _build_machine_health_check_cr(_req())["spec"]

        assert spec["nodeStartupTimeout"] == "20m"

    def test_max_unhealthy_still_allows_the_only_worker_to_be_replaced(self) -> None:
        """A one-worker tenant is the common case; the usual 40% guard refuses
        to remediate exactly when the tenant is fully down."""
        spec = _build_machine_health_check_cr(_req())["spec"]

        assert spec["maxUnhealthy"] == "100%"

    def test_remediation_is_still_configured_at_all(self) -> None:
        """Widening the window far enough is indistinguishable from removing
        the health check; this is the guard against drifting there."""
        minutes = int(WORKER_UNHEALTHY_TIMEOUT.removesuffix("m"))
        startup = int(_build_machine_health_check_cr(_req())["spec"]
                      ["nodeStartupTimeout"].removesuffix("m"))

        assert minutes < startup
