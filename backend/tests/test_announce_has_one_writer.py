"""The FRRConfiguration takes exactly one writer.

frr-k8s merges every configuration in its namespace into the node's FRR, so two
writers of that object are not two opinions — they are two `router bgp` blocks
fighting over one session. The reconcile loop therefore steps aside completely
when the operator owns the announcements, rather than both of them writing and
hoping they agree.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _run_pass(*, operator_owns: bool, monkeypatch: pytest.MonkeyPatch) -> int:
    from app.core import tenant_reconciler

    monkeypatch.setenv("OPERATOR_ANNOUNCE_ENABLED", "true" if operator_owns else "")

    announced = AsyncMock()
    with (
        patch.object(tenant_reconciler, "ensure_announcements", announced),
        patch.object(tenant_reconciler, "_repair_worker_templates", AsyncMock(), create=True),
        patch.object(tenant_reconciler, "_reconcile_addons", AsyncMock(), create=True),
    ):
        try:
            await tenant_reconciler._reconcile_once(MagicMock())
        except Exception:
            # The rest of the pass needs a real cluster; only the announcement
            # decision is under test, and it happens first.
            pass
    return announced.await_count


@pytest.mark.asyncio
class TestOwnershipIsExclusive:
    async def test_the_loop_writes_when_it_owns_them(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert await _run_pass(operator_owns=False, monkeypatch=monkeypatch) == 1

    async def test_the_loop_steps_aside_when_the_operator_owns_them(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not "writes something compatible" — writes nothing at all."""
        assert await _run_pass(operator_owns=True, monkeypatch=monkeypatch) == 0
