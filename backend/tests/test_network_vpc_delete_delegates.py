"""There were two VPC deletes, and only one of them was fixed.

`DELETE /api/v1/vpcs/{name}` learned to wait for kube-ovn before removing the
router (2026.10.12), after `snat-gamma-net`, `eip-gamma-net` and
`con3-default` were stranded permanently on the lab cluster. The older
`DELETE /api/v1/network/vpcs/{name}` kept its own copy:

    for s in subnets: delete subnet
    delete vpc                      # same breath, same stranding

and that copy also skipped the VpcDns CR, the Kyverno policy, the NAT
objects and the isolation re-scope. The UI calls the fixed route, so the
divergence was invisible from the screen — which is exactly how it would have
survived.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC = Path("app/api/v1/network.py").read_text()
BODY = SRC[SRC.index('@router.delete("/vpcs/{name}")'):SRC.index("# VPC Peering")]


def test_the_older_route_no_longer_carries_its_own_teardown() -> None:
    assert 'plural="subnets"' not in BODY, "deleting subnets here is the duplicated teardown"
    assert 'plural="vpcs"' not in BODY


def test_it_delegates_to_the_maintained_one() -> None:
    assert "from app.api.v1.vpcs import delete_vpc" in BODY


def test_it_passes_the_user_through() -> None:
    """`Depends` is resolved for routed requests only; a sibling handler
    called without `user=` receives the Depends object and dies on
    `'Depends' object has no attribute 'groups'` — which is how three
    released features broke before."""
    assert "user=user" in BODY


@pytest.mark.asyncio
async def test_the_call_actually_reaches_the_guarded_teardown(monkeypatch):
    from app.api.v1 import network as mod

    delegate = AsyncMock(return_value={"status": "deleted", "name": "t1"})
    monkeypatch.setattr("app.api.v1.vpcs.delete_vpc", delegate)

    request = MagicMock()
    user = MagicMock()
    user.groups = ["kubevirt-ui-admins"]

    result = await mod.delete_vpc(request, "t1", user=user)

    assert result["status"] == "deleted"
    delegate.assert_awaited_once()
    assert delegate.await_args.kwargs["user"] is user, "not a Depends placeholder"
