"""Which VPCs a user may see — decided once, for every reader.

There were two rules. `GET /vpcs` asked "does the VPC's `spec.namespaces`
overlap the user's namespaces, **or** does the folder access block admit
them"; `GET /subnets` asked only the first half, and dropped every subnet
whose VPC failed it.

The second half is not a nicety. A VPC that has just been created has an
empty `spec.namespaces` — nothing is attached to it yet — so the overlap test
alone hides a brand-new VPC from exactly the person who is meant to pick it.
That was found once and fixed in `list_vpcs`, with a comment explaining it,
and `_user_visible_vpc_names` was never told.

Measured on the stand, run 4: three VPCs in `poc-transit/dev`, all with
`namespaces: []`. The tenant wizard offered all three and built a tenant in
one of them; the VM wizard said "No VPC or external networks available for
this project" — one backend, one user, one set of objects, two answers. The
VM path to a custom VPC was unreachable through the UI, which took the whole
E phase of the run with it.

So the rule lives here and both call it. Not because duplication is untidy —
because the halves drifted apart for as long as nobody compared them.
"""

from dataclasses import dataclass
from typing import Any, Iterable

from fastapi import HTTPException

from app.core.groups import (
    get_user_namespaces,
    is_admin,
    is_env_viewer,
    is_folder_viewer,
    load_folders,
)


@dataclass(frozen=True)
class VpcFacts:
    """The only things about a VPC that decide who may see it."""

    name: str
    folder: str | None = None
    environment: str | None = None
    namespaces: tuple[str, ...] = ()


def _accessible(
    user: Any, vpc: VpcFacts, user_ns: set[str], folders: dict[str, dict],
) -> bool:
    """The rule itself, with everything already read.

    Two ways in, and either is enough:

      * the VPC is bound to a namespace the user can reach — the original
        test, and the only one that works for a VPC in use;
      * the folder access block admits them — a folder-wide VPC needs a
        folder viewer, an env-scoped one needs a viewer of that env. This is
        what makes an empty, freshly created VPC visible to the person who
        will attach something to it.

    An unlabelled VPC has no folder to ask about, so it stays admin-only.
    """
    if set(vpc.namespaces) & user_ns:
        return True
    if not vpc.folder:
        return False
    folder_meta = folders.get(vpc.folder)
    if not folder_meta:
        return False
    if vpc.environment is None:
        return is_folder_viewer(user, folder_meta)
    return is_env_viewer(user, folder_meta, vpc.environment)


async def visible_vpcs(
    k8s: Any, user: Any, vpcs: Iterable[VpcFacts], system_vpc: str,
) -> set[str]:
    """The names of the VPCs this user may see, the system one never among them.

    Admins are not special-cased here: a caller that wants "everything" should
    not be asking a filter. Both current callers check `is_admin` first, and
    passing an admin through this would quietly hide any VPC whose folder no
    longer exists.
    """
    user_ns = set(await get_user_namespaces(k8s, user))
    try:
        folders = await load_folders(k8s)
    except HTTPException as e:
        # 404 → no folders configured yet, so there is no label-based access
        # to grant; fall back to namespace overlap alone. Anything else is a
        # real failure and must not read as "you may see nothing".
        if e.status_code != 404:
            raise
        folders = {}

    return {
        vpc.name
        for vpc in vpcs
        if vpc.name and vpc.name != system_vpc
        and _accessible(user, vpc, user_ns, folders)
    }


def facts_from_item(item: dict) -> VpcFacts:
    """VpcFacts from a raw kube-ovn VPC object."""
    meta = item.get("metadata") or {}
    labels = meta.get("labels") or {}
    return VpcFacts(
        name=meta.get("name", ""),
        folder=labels.get("kubevirt-ui.io/folder") or None,
        environment=labels.get("kubevirt-ui.io/environment") or None,
        namespaces=tuple((item.get("spec") or {}).get("namespaces") or ()),
    )
