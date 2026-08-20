"""Who repairs a worker template that has no Kubernetes CA.

One of them, not both. The repair itself is harmless to run twice —
create-if-absent and a patch to the same value — but two writers of one thing is
what this migration exists to remove, and a flag that only half-works is how a
cutover goes wrong quietly.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_the_loop_repairs_while_the_operator_does_not():
    from app.core import tenant_reconciler

    repair = AsyncMock()
    with patch.object(tenant_reconciler, "ensure_worker_bootstrap_ca", repair), \
         patch.object(tenant_reconciler, "tenant_bootstrap_path_enabled", return_value=False):
        did = await tenant_reconciler._repair_worker_bootstrap(AsyncMock())

    assert did is True
    assert repair.await_count == 1


@pytest.mark.asyncio
async def test_the_loop_stands_aside_once_the_operator_has_it():
    from app.core import tenant_reconciler

    repair = AsyncMock()
    with patch.object(tenant_reconciler, "ensure_worker_bootstrap_ca", repair), \
         patch.object(tenant_reconciler, "tenant_bootstrap_path_enabled", return_value=True):
        did = await tenant_reconciler._repair_worker_bootstrap(AsyncMock())

    assert did is False
    assert repair.await_count == 0, "both halves repaired the same templates"


@pytest.mark.asyncio
async def test_the_pass_still_calls_it():
    """The guard being right matters less than the pass using it, and a helper
    nobody calls is a guard nobody has."""
    import inspect

    from app.core import tenant_reconciler

    source = inspect.getsource(tenant_reconciler._reconcile_once)
    assert "_repair_worker_bootstrap(k8s)" in source
