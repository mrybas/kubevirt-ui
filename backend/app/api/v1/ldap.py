"""LDAP API endpoints (Phase 2).

`GET /api/v1/ldap/groups?q=...` is used by the folder Access tab to
autocomplete group names when assigning folder admin / member / viewer.

Open to any authenticated user — group names are not sensitive (they
already appear in the user's own OIDC `groups` claim).  Folder admins
need this for autocomplete, and per-folder gating would require a
folder path the UI doesn't have.
"""

import logging

from fastapi import APIRouter, Depends, Query

from app.core.auth import User, require_auth
from app.core.ldap import is_external_ldap_configured, search_groups
from app.core.lldap_client import LLDAP_ENABLED

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/groups")
async def list_ldap_groups(
    q: str = Query("", description="Substring search on group name (cn)"),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_auth),
) -> dict:
    """Search the directory for group names.

    Returns `{"items": ["group-1", ...]}`.  Empty list when no backend
    is configured — the UI falls back to free-text group entry.
    """
    items = await search_groups(q, limit=limit)
    return {
        "items": items,
        "backend": (
            "ldap" if is_external_ldap_configured()
            else "lldap" if LLDAP_ENABLED
            else "none"
        ),
    }
