"""LDAP group search service (Phase 2).

Backs the `/api/v1/ldap/groups?q=...` endpoint used by the folder Access
tab to autocomplete FreeIPA / external LDAP group names when assigning
folder admin / member / viewer roles.

Two backends are tried, in priority order:
  1. **External LDAP** (FreeIPA, etc.) — used when `LDAP_URL` is set.
     Reuses the same readonly bind that Dex uses; admins point this at
     the same Kubernetes Secret via the chart (see helm
     `backend.extraEnv` / `backend.envFrom`).
  2. **Bundled LLDAP** — used when `LLDAP_ENABLED=true` and no external
     LDAP is configured.  Lists groups via the LLDAP GraphQL admin API.

If neither is configured, `search_groups` returns an empty list — the
endpoint is intentionally degradable so the UI keeps working (the group
input just falls back to free-text entry).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Configuration — sourced from env vars on the backend pod.  Operators
# wire these from the same Secret used by Dex's LDAP connector.
LDAP_URL = os.getenv("LDAP_URL", "")                    # e.g. ldaps://ipa.example.com:636
LDAP_BIND_DN = os.getenv("LDAP_BIND_DN", "")            # e.g. uid=readonly,cn=users,...
LDAP_BIND_PASSWORD = os.getenv("LDAP_BIND_PW", "") or os.getenv("LDAP_BIND_PASSWORD", "")
LDAP_GROUP_BASE_DN = os.getenv("LDAP_GROUP_BASE_DN", "")  # e.g. cn=groups,cn=accounts,dc=example,dc=com
LDAP_GROUP_FILTER = os.getenv(
    "LDAP_GROUP_FILTER",
    "(&(objectClass=groupOfNames)(cn=*{q}*))",
)
LDAP_GROUP_NAME_ATTR = os.getenv("LDAP_GROUP_NAME_ATTR", "cn")
# Use STARTTLS on a plain ldap:// connection (FreeIPA's typical setup).
LDAP_USE_STARTTLS = os.getenv("LDAP_USE_STARTTLS", "false").lower() in ("true", "1", "yes")


def is_external_ldap_configured() -> bool:
    """True when external LDAP is wired up (URL set)."""
    return bool(LDAP_URL)


def _escape_ldap_filter(value: str) -> str:
    """Escape special characters in an LDAP filter value (RFC 4515).

    Prevents filter injection from user-supplied search queries.
    """
    out = []
    for ch in value:
        code = ord(ch)
        if ch in ("*", "(", ")", "\\", "\0"):
            out.append("\\%02x" % code)
        elif code < 0x20:
            out.append("\\%02x" % code)
        else:
            out.append(ch)
    return "".join(out)


def _search_groups_sync(query: str, limit: int) -> list[str]:
    """Blocking LDAP search — must run in a thread (called via to_thread)."""
    try:
        import ldap3
    except ImportError:
        logger.warning("ldap3 not installed; LDAP group search disabled")
        return []

    if not LDAP_URL or not LDAP_GROUP_BASE_DN:
        return []

    try:
        server = ldap3.Server(LDAP_URL, get_info=ldap3.NONE)
        conn = ldap3.Connection(
            server,
            user=LDAP_BIND_DN or None,
            password=LDAP_BIND_PASSWORD or None,
            auto_bind=False,
            receive_timeout=10,
        )
        if LDAP_USE_STARTTLS:
            conn.start_tls()
        if not conn.bind():
            logger.warning(f"LDAP bind failed: {conn.result}")
            return []

        ldap_filter = LDAP_GROUP_FILTER.format(q=_escape_ldap_filter(query))
        conn.search(
            search_base=LDAP_GROUP_BASE_DN,
            search_filter=ldap_filter,
            search_scope=ldap3.SUBTREE,
            attributes=[LDAP_GROUP_NAME_ATTR],
            size_limit=limit,
            time_limit=5,
        )
        groups: list[str] = []
        for entry in conn.entries:
            attr = getattr(entry, LDAP_GROUP_NAME_ATTR, None)
            if attr is None:
                continue
            # attr.values is a list (multi-valued attrs); use the first.
            values = attr.values if hasattr(attr, "values") else [attr.value]
            for v in values:
                if v and v not in groups:
                    groups.append(str(v))
                    if len(groups) >= limit:
                        break
            if len(groups) >= limit:
                break
        try:
            conn.unbind()
        except Exception:
            pass
        return groups
    except Exception as e:
        logger.warning(f"LDAP group search failed: {e}")
        return []


async def _search_groups_lldap(query: str, limit: int) -> list[str]:
    """Fallback: list groups via the bundled LLDAP GraphQL admin API.

    LLDAP does not support server-side substring filtering, so we list
    all groups and filter in-process.  The result set is small (tens of
    groups in bundled mode), so the cost is negligible.
    """
    from app.core.lldap_client import LLDAP_ENABLED, get_lldap_client

    if not LLDAP_ENABLED:
        return []

    try:
        client = get_lldap_client()
        groups = await client.list_groups()
    except Exception as e:
        logger.warning(f"LLDAP group listing failed: {e}")
        return []

    needle = query.lower()
    result: list[str] = []
    for g in groups:
        name = g.get("displayName") or g.get("name") or ""
        if not name or name == "lldap_admin":
            continue
        if not needle or needle in name.lower():
            result.append(name)
            if len(result) >= limit:
                break
    return result


async def search_groups(query: str, limit: int = 20) -> list[str]:
    """Search groups by substring match on the group name (`cn`).

    Returns a list of bare group names (the same names the user's OIDC
    `groups` claim would contain).  Up to `limit` entries.  Empty list
    when no backend is configured or on any error — endpoint stays usable.
    """
    if limit <= 0:
        return []
    # Cap to a sane upper bound to avoid abusive queries.
    limit = min(limit, 100)

    if is_external_ldap_configured():
        # Run blocking ldap3 call in a thread so we don't stall the loop.
        return await asyncio.to_thread(_search_groups_sync, query, limit)

    return await _search_groups_lldap(query, limit)
