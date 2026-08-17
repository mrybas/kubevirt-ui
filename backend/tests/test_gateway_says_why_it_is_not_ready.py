"""`Not Ready` without a cause is a dead end.

On the lab the gateway sat Not Ready for a while and the badge said nothing.
The answer was one `kubectl describe pod` away — `AcquireAddressFailed`, the
external subnet had no free address left — but that command needs cluster
credentials, which is precisely what the person reading the UI does not have.

So the API carries the cause: the newest failing condition on the
VpcEgressGateway if there is one, otherwise the pod's own waiting reason, and
failing that the newest Warning event about the pod. And it carries it only
while the gateway is actually not ready — a stale explanation next to a green
badge is its own kind of lie.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from app.api.v1.egress_gateway import _not_ready_cause, _parse_gateway

GW = "shared-egress"


def _k8s(pods: list | None = None, events: list | None = None) -> MagicMock:
    async def list_pods(**kw):
        r = MagicMock()
        r.items = pods or []
        return r

    async def list_events(**kw):
        r = MagicMock()
        r.items = events or []
        return r

    k8s = MagicMock()
    k8s.core_api.list_namespaced_pod = AsyncMock(side_effect=list_pods)
    k8s.core_api.list_namespaced_event = AsyncMock(side_effect=list_events)
    return k8s


def _pod(name: str, phase: str, waiting: tuple[str, str] | None = None):
    p = MagicMock()
    p.metadata.name = name
    p.status.phase = phase
    if waiting is None:
        p.status.container_statuses = []
    else:
        reason, message = waiting
        cs = MagicMock()
        cs.state.waiting.reason = reason
        cs.state.waiting.message = message
        p.status.container_statuses = [cs]
    return p


def _event(name: str, type_: str, reason: str, message: str, ts: str):
    e = MagicMock()
    e.type = type_
    e.reason = reason
    e.message = message
    e.last_timestamp = ts
    return e


def _veg(ready: bool, conditions: list[dict] | None = None) -> dict:
    return {"spec": {}, "status": {"ready": ready, "conditions": conditions or []}}


class TestTheCauseReachesTheAPI:
    @pytest.mark.asyncio
    async def test_a_failing_condition_is_reported_verbatim(self) -> None:
        veg = _veg(False, [{
            "type": "Ready", "status": "False",
            "reason": "AcquireAddressFailed",
            "message": "no available IP in subnet external",
            "lastTransitionTime": "2026-08-17T10:00:00Z",
        }])

        cause = await _not_ready_cause(_k8s(), GW, veg)

        assert cause == "AcquireAddressFailed: no available IP in subnet external"

    @pytest.mark.asyncio
    async def test_the_newest_failure_wins_over_an_older_one(self) -> None:
        veg = _veg(False, [
            {"type": "Validated", "status": "False", "reason": "Old",
             "message": "yesterday", "lastTransitionTime": "2026-08-16T10:00:00Z"},
            {"type": "Ready", "status": "False", "reason": "New",
             "message": "today", "lastTransitionTime": "2026-08-17T10:00:00Z"},
        ])

        assert await _not_ready_cause(_k8s(), GW, veg) == "New: today"

    @pytest.mark.asyncio
    async def test_a_pod_stuck_waiting_explains_itself(self) -> None:
        k8s = _k8s(pods=[_pod(
            "shared-egress-0", "Pending",
            ("CreateContainerConfigError", "secret 'gw-conf' not found"),
        )])

        cause = await _not_ready_cause(k8s, GW, _veg(False))

        assert "shared-egress-0" in cause
        assert "CreateContainerConfigError" in cause
        assert "secret 'gw-conf' not found" in cause

    @pytest.mark.asyncio
    async def test_without_container_state_the_warning_event_answers(self) -> None:
        """The pod never got far enough to have a container — ask the events.

        This is the unschedulable case: the external-gw node label drifted and
        nothing can host the gateway. The spec is flawless; only the scheduler
        knows.
        """
        k8s = _k8s(
            pods=[_pod("shared-egress-0", "Pending")],
            events=[
                _event("shared-egress-0", "Normal", "Scheduled", "ok", "2026-08-17T09:00:00Z"),
                _event("shared-egress-0", "Warning", "FailedScheduling",
                       "0/3 nodes match node selector", "2026-08-17T10:00:00Z"),
            ],
        )

        cause = await _not_ready_cause(k8s, GW, _veg(False))

        assert "FailedScheduling" in cause
        assert "0/3 nodes match node selector" in cause

    @pytest.mark.asyncio
    async def test_a_missing_vpc_egress_gateway_is_named_as_such(self) -> None:
        """The VPC exists, nothing drives it — the emptiest kind of Not Ready."""
        cause = await _not_ready_cause(_k8s(), GW, None)

        assert "VpcEgressGateway is missing" in cause

    @pytest.mark.asyncio
    async def test_running_pods_and_no_failing_condition_yield_nothing(self) -> None:
        """Mid-rollout is not a fault; do not invent a cause for it."""
        k8s = _k8s(pods=[_pod("shared-egress-0", "Running")])

        assert await _not_ready_cause(k8s, GW, _veg(False)) is None

    @pytest.mark.asyncio
    async def test_an_events_api_failure_does_not_break_the_listing(self) -> None:
        k8s = _k8s(pods=[_pod("shared-egress-0", "Pending")])
        k8s.core_api.list_namespaced_event = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden")
        )

        cause = await _not_ready_cause(k8s, GW, _veg(False))

        assert "shared-egress-0 is Pending" in cause


class TestTheCauseDoesNotOutliveTheFault:
    def test_a_ready_gateway_carries_no_explanation(self) -> None:
        """Otherwise the sentence lingers next to a green badge and misleads."""
        gw = _parse_gateway(
            {"metadata": {"name": "egw-shared-egress", "labels": {"kubevirt-ui.io/egress-gateway": GW}}},
            _veg(True), [], [], None,
            "AcquireAddressFailed: no available IP in subnet external",
        )

        assert gw.ready is True
        assert gw.not_ready_reason is None

    def test_a_not_ready_gateway_keeps_it(self) -> None:
        gw = _parse_gateway(
            {"metadata": {"name": "egw-shared-egress", "labels": {"kubevirt-ui.io/egress-gateway": GW}}},
            _veg(False), [], [], None,
            "AcquireAddressFailed: no available IP in subnet external",
        )

        assert gw.ready is False
        assert gw.not_ready_reason.startswith("AcquireAddressFailed")
