"""Harbor management API client.

Unlike app/core/lldap_client.py, this client holds NO service credential.
Every method takes the caller's bearer token as its first argument, so there
is no code path that can reach Harbor's management API as a shared identity.

Measured on Harbor 2.15.2: a dex-issued id_token works as an API bearer and
Harbor applies that user's roles. Robot accounts do NOT work here at all, at
either level — they are for the registry API (pull and push) only.
"""

import logging
import os
from typing import Any
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger(__name__)

HARBOR_URL = os.getenv("HARBOR_URL", "").rstrip("/")
HARBOR_TIMEOUT_SECONDS = float(os.getenv("HARBOR_TIMEOUT_SECONDS", "10"))


def harbor_registry_host() -> str:
    """The bare host[:port] a push or pull ref resolves against.

    Harbor exposes the management API and the registry on the same host; only
    the path differs (`/api/v2.0/...` versus the registry's own root).
    HARBOR_URL carries a scheme because httpx needs one — crane and Docker
    image refs never do, so this strips it rather than making every caller
    remember to.
    """
    parsed = urlparse(HARBOR_URL)
    return parsed.netloc or parsed.path


class HarborUnavailable(Exception):
    """Harbor could not be reached, or answered 5xx."""


class HarborUnauthorized(Exception):
    """Harbor rejected the caller's token."""


class HarborClient:
    """Read-only client for Harbor's management API."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or HARBOR_URL).rstrip("/")
        self._transport: httpx.AsyncBaseTransport | None = None

    async def _get(self, token: str, path: str) -> list[dict[str, Any]]:
        url = f"{self._base_url}/api/v2.0{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=HARBOR_TIMEOUT_SECONDS
            ) as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.HTTPError as exc:
            raise HarborUnavailable(str(exc)) from exc

        if resp.status_code in (401, 403):
            raise HarborUnauthorized(f"Harbor rejected the token for {path}")
        if resp.status_code >= 400:
            # Any other 4xx (404 not-found, 400, 409, 422, ...) and any 5xx.
            # HarborUnauthorized/HarborUnavailable are meant to be exhaustive
            # for callers — a bare httpx.HTTPStatusError must never escape.
            raise HarborUnavailable(f"Harbor returned {resp.status_code} for {path}")

        try:
            body = resp.json()
        except ValueError as exc:
            # A 2xx with an empty or non-JSON body is equally undesigned —
            # fold it into the same exhaustive exception surface.
            raise HarborUnavailable(
                f"Harbor returned an unparseable body for {path}"
            ) from exc

        return body if isinstance(body, list) else []

    async def list_projects(self, token: str) -> list[dict[str, Any]]:
        return await self._get(token, "/projects?page_size=100")

    async def list_repositories(self, token: str, project: str) -> list[dict[str, Any]]:
        project = quote(project, safe="")
        return await self._get(token, f"/projects/{project}/repositories?page_size=100")

    async def list_artifacts(
        self, token: str, project: str, repository: str
    ) -> list[dict[str, Any]]:
        project = quote(project, safe="")
        repository = quote(repository, safe="")
        return await self._get(
            token,
            f"/projects/{project}/repositories/{repository}/artifacts?page_size=100",
        )
