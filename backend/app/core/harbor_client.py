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

# How many rows a single page asks for, and how many pages will ever be
# followed. Harbor caps page_size at 100, so asking for more silently gets
# 100 back — the cap is here so a client and a server that disagree do not
# turn into a loop that never ends.
HARBOR_PAGE_SIZE = 100
HARBOR_MAX_PAGES = 1000


def harbor_robot_secret_name() -> str:
    """Name of the Secret holding the tenant's Harbor robot credential.

    Read from the environment on every call rather than captured at import,
    so a deployment (and a test) can change it without reloading the module.

    The convention is one Secret name, the SAME in every tenant namespace —
    what makes it "the tenant's" is which namespace it is in, which is also
    the only namespace CDI will look in. That is deliberately the one thing
    the browser never needs to know: a credential name in a page is a
    credential name in a bug report.
    """
    return os.getenv("HARBOR_ROBOT_SECRET", "harbor-robot")


def harbor_ca_configmap_name() -> str:
    """Name of the ConfigMap holding Harbor's CA, when it uses a private one.

    Unlike the robot Secret this is optional: a Harbor behind a publicly
    trusted certificate needs no ConfigMap at all, and CDI fails an import
    outright if `certConfigMap` names something that is not there. Callers
    therefore attach it only when a ConfigMap by this name actually exists in
    the target namespace. Set to the empty string to switch the convention
    off entirely.
    """
    return os.getenv("HARBOR_CA_CONFIGMAP", "harbor-ca")


def harbor_registry_host() -> str:
    """The bare host[:port] a push or pull ref resolves against.

    Harbor exposes the management API and the registry on the same host; only
    the path differs (`/api/v2.0/...` versus the registry's own root).
    HARBOR_URL carries a scheme because httpx needs one — crane and Docker
    image refs never do, so this strips it rather than making every caller
    remember to.

    Read from the environment on every call, defaulting to the value captured
    at import. The module constant alone would make this untestable and, worse,
    silently stale for anything that sets HARBOR_URL after import — and this
    is now the single source of the registry host for BOTH directions, push
    and pull, so being wrong here is being wrong everywhere.
    """
    parsed = urlparse(os.getenv("HARBOR_URL", HARBOR_URL).rstrip("/"))
    return parsed.netloc or parsed.path


class HarborUnavailable(Exception):
    """Harbor could not be reached, or answered 5xx."""


class HarborUnauthorized(Exception):
    """Harbor rejected the caller's token."""


class HarborNotFound(HarborUnavailable):
    """Harbor answered 404 — the project, repository or artifact is not there.

    A subclass of HarborUnavailable on purpose: every existing caller catches
    the two designed exceptions and degrades, and a 404 must keep taking that
    path for them. It exists so the one caller that CARES can tell the
    difference — `assert_tag_is_free` asks a repository that has never been
    pushed to for its artifacts, and Harbor's answer to that is 404. Read as
    an outage it makes the first publish to any new repository a guaranteed
    500; read as "nothing there", it means exactly what it should: the tag is
    free.
    """


class HarborClient:
    """Read-only client for Harbor's management API."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or HARBOR_URL).rstrip("/")
        self._transport: httpx.AsyncBaseTransport | None = None

    @staticmethod
    def _rows(resp: httpx.Response, path: str) -> list[dict[str, Any]]:
        """The JSON list in a checked response, or the designed exception."""
        if resp.status_code in (401, 403):
            raise HarborUnauthorized(f"Harbor rejected the token for {path}")
        if resp.status_code == 404:
            # Distinguished from every other error only so `assert_tag_is_free`
            # can read it as "nothing pushed here yet". Still a subclass of
            # HarborUnavailable, so callers that catch the two designed
            # exceptions are unaffected.
            raise HarborNotFound(f"Harbor returned 404 for {path}")
        if resp.status_code >= 400:
            # Any other 4xx (400, 409, 422, ...) and any 5xx.
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

    @staticmethod
    def _has_another_page(resp: httpx.Response, fetched: int, got: int) -> bool:
        """Whether a further page exists, by the most reliable signal present.

        Harbor paginates every list endpoint and returns only the first page
        unless asked otherwise. Reading page one alone is not a performance
        shortcut, it is wrong: `assert_tag_is_free` walks this list to decide
        whether a tag is taken, and a tag sitting on page two would be
        reported free — publishing over it, which CDI then never re-imports.

        `Link: <...>; rel="next"` is the strongest signal because it is
        Harbor's own statement about its own cursor; a Link header WITHOUT a
        next relation is an equally definite statement that this is the last
        page. `X-Total-Count` is the fallback, and a full page the last
        resort (a full final page then costs one extra empty request, which
        is the cheap way to be wrong).
        """
        link = resp.headers.get("link") or ""
        if link:
            return 'rel="next"' in link or "rel=next" in link

        total = resp.headers.get("x-total-count")
        if total is not None:
            try:
                return fetched < int(total)
            except ValueError:
                pass

        return got >= HARBOR_PAGE_SIZE

    async def _get(self, token: str, path: str) -> list[dict[str, Any]]:
        """Every row on every page of a Harbor list endpoint."""
        sep = "&" if "?" in path else "?"
        rows: list[dict[str, Any]] = []

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=HARBOR_TIMEOUT_SECONDS
            ) as client:
                for page in range(1, HARBOR_MAX_PAGES + 1):
                    url = (
                        f"{self._base_url}/api/v2.0{path}{sep}"
                        f"page={page}&page_size={HARBOR_PAGE_SIZE}"
                    )
                    try:
                        resp = await client.get(
                            url, headers={"Authorization": f"Bearer {token}"}
                        )
                    except httpx.HTTPError as exc:
                        raise HarborUnavailable(str(exc)) from exc

                    got = self._rows(resp, path)
                    rows.extend(got)
                    if not self._has_another_page(resp, len(rows), len(got)):
                        break
                else:
                    logger.warning(
                        "harbor: stopped following %s after %d pages (%d rows)",
                        path, HARBOR_MAX_PAGES, len(rows),
                    )
        except httpx.HTTPError as exc:
            # A transport failure raised while opening or closing the client
            # rather than during a request.
            raise HarborUnavailable(str(exc)) from exc

        return rows

    async def verify_identity(self, token: str) -> None:
        """Confirm the bearer names a real Harbor identity before listing anything.

        `GET /projects` — where catalog_images() used to start — returns 200
        for ANY bearer, garbage or absent included: it just filters to what
        that identity can see, which is empty for an anonymous caller. A
        wrong identity and a legitimately empty catalogue are indistinguishable
        from that endpoint alone. Measured against a real Harbor 2.15.2 (Task
        9's e2e run) — the earlier belief that /projects/{x}/repositories's
        401 generalised to /projects was the bug this method exists to close.

        `GET /users/current` is auth-gated and does not have that problem:

            401/403  no bearer, or one Harbor does not recognise      -> reject
            200      a Dex-issued id_token, mapped to its OIDC user   -> proceed
            412      a robot account — a real identity, just not a
                     user account (this API is for browsing anyway,
                     which robots do not do)                          -> proceed

        Raises HarborUnauthorized or HarborUnavailable; returns None on
        success. Callers must run this before enumerating anything.
        """
        url = f"{self._base_url}/api/v2.0/users/current"
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
            raise HarborUnauthorized("Harbor rejected the caller's identity")
        if resp.status_code == 200 or resp.status_code == 412:
            # 412: recognised as a robot account, not a rejection.
            return
        raise HarborUnavailable(
            f"Harbor returned {resp.status_code} for /users/current"
        )

    async def list_projects(self, token: str) -> list[dict[str, Any]]:
        return await self._get(token, "/projects")

    async def list_repositories(self, token: str, project: str) -> list[dict[str, Any]]:
        project = quote(project, safe="")
        return await self._get(token, f"/projects/{project}/repositories")

    async def list_project_artifacts(
        self, token: str, project: str
    ) -> list[dict[str, Any]]:
        """Every artifact in one project, in one paginated call.

        Replaces the repositories-then-artifacts walk for catalogue browsing:
        each Artifact in this response carries `repository_name`, which is the
        only thing the per-repository round trips were fetching. That turns
        `1 + P + (P x R)` requests per catalogue read into `1 + P`.

        MEASURED against the lab Harbor with a valid user OIDC token: 200 on
        a public AND a private project, 401 with no token or a garbage bearer.
        Authorisation is genuinely enforced here — better than `/projects`,
        which answers 200 to anyone, and which is why `verify_identity()`
        exists. `repository_name` is populated on every real artifact, not
        merely declared in the schema, and pagination behaves normally
        (`X-Total-Count` plus `Link: rel="next"`).

        DO NOT ADD `latest_in_repository=true`. The documentation offers it as
        the way to get one current artifact per repository instead of every
        tag, and on this Harbor build it does not work at all. Measured
        against real pushed artifacts:

            ?latest_in_repository=true            -> HTTP 400, "either
              'media_type' or 'artifact_type' must be specified, but not
              both, when querying with latest_in_repository"
            + the companion filter via `q=`       -> HTTP 500 for the brace
              and fuzzy forms; 200 with ZERO results for the bare form, on
              artifacts that unambiguously match the values queried

        No syntax was found that returns a correct non-empty result. Sending
        it would 400 in production or, worse, return a silently empty
        catalogue. Note that dropping it costs no requests: the `1 + P`
        saving comes from calling the PROJECT-WIDE endpoint rather than
        per-repository ones, and `latest_in_repository` only ever reduced the
        number of rows. Listing every tag is also exactly what the code on
        `main` does today, so there is no behaviour change either.

        `list_repositories`/`list_artifacts` below are kept and still used —
        publish reads one repository's tags through `list_artifacts` — so
        reverting to the walk stays a small change.
        """
        project = quote(project, safe="")
        return await self._get(token, f"/projects/{project}/artifacts")

    async def list_artifacts(
        self, token: str, project: str, repository: str
    ) -> list[dict[str, Any]]:
        project = quote(project, safe="")
        repository = quote(repository, safe="")
        return await self._get(
            token,
            f"/projects/{project}/repositories/{repository}/artifacts",
        )
