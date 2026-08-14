"""Deleting a VPC has to take its subnets and the other side of its peerings.

`delete_vpc` unpacked `_get_vpc_subnets`, which returns `(subnets, isolated)`,
straight into a `for`:

    AttributeError: 'list' object has no attribute 'name'

so the request answered 500, the subnet stayed, and kube-ovn could then never
finish deleting the VPC — the exact mess that has to be untangled by hand with
finalizers.

It also tried to delete `vpc-peerings` objects, which do not exist: a peering
is an entry in `Vpc.spec.vpcPeerings` on *both* routers, and the remote side
was left pointing at a VPC that no longer exists.
"""

from pathlib import Path

SRC = Path("app/api/v1/vpcs.py").read_text()
BODY = SRC[SRC.index("async def delete_vpc("):SRC.index("\n@router.", SRC.index("async def delete_vpc("))]


def test_the_subnet_list_is_unpacked() -> None:
    assert "subnets, _isolated = await _get_vpc_subnets(k8s, name)" in BODY


def test_it_no_longer_deletes_a_kind_that_does_not_exist() -> None:
    assert '"vpc-peerings"' not in BODY


def test_the_remote_side_of_each_peering_is_removed() -> None:
    assert "_remove_peering_side(k8s, remote, name)" in BODY


def test_the_isolation_rules_are_re_scoped_afterwards() -> None:
    assert "reconcile_isolation_acls(k8s)" in BODY
