"""The scope has to survive serialisation, not just exist on the model.

`/api/v1/auth/config` is served by `AuthConfigResponse`, a second model that
`get_config()` fills in field by field. A field added to `AuthConfig` but not
listed there is dropped silently at the API boundary: the endpoint still
answers 200, the frontend's `config.scope` is simply `undefined`, so it falls
back to its hardcoded default. The token is then minted without the peer
audience, and Harbor answers 401 to every call -- which the UI reports as an
unreachable image catalogue rather than as an auth failure.

The existing scope tests assert against `AuthConfig`, the internal model, and
so cannot catch this. These assert against the actual HTTP response.
"""

import pytest
from fastapi.testclient import TestClient

import app.core.auth as auth


@pytest.fixture
def oidc_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin OIDC settings and stub discovery so /config takes the oidc path."""
    monkeypatch.setattr(auth, "OIDC_ISSUER", "https://dex.example/dex")
    monkeypatch.setattr(auth, "OIDC_CLIENT_ID", "kubevirt-ui")

    async def _discovery() -> dict[str, str]:
        return {
            "authorization_endpoint": "https://dex.example/dex/auth",
            "token_endpoint": "https://dex.example/dex/token",
            "userinfo_endpoint": "https://dex.example/dex/userinfo",
        }

    monkeypatch.setattr(auth, "get_oidc_config", _discovery)


def test_the_served_config_carries_the_configured_scope(
    client: TestClient, oidc_discovered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the field the frontend builds its authorize URL from."""
    want = "openid profile email groups audience:server:client_id:harbor"
    monkeypatch.setattr(auth, "OIDC_SCOPE", want)

    body = client.get("/api/v1/auth/config").json()

    assert body.get("scope") == want, (
        "scope missing from the served config: the frontend falls back to its "
        "default and never asks for the peer audience"
    )


def test_the_served_config_carries_the_default_scope(
    client: TestClient, oidc_discovered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfigured deployment must still be told what it is asking for."""
    monkeypatch.setattr(auth, "OIDC_SCOPE", "openid profile email groups")

    body = client.get("/api/v1/auth/config").json()

    assert body.get("scope") == "openid profile email groups"
