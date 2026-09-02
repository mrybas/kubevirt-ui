"""The login scope has to be configurable, or cross-client auth is impossible.

A token dex mints for this client is refused by any other relying party that
validates its own audience: Harbor answers 401 on every call, which reads as a
permissions problem and is not one. The only fix at the protocol level is to
ask for `audience:server:client_id:<peer>` at login, which means the scope
cannot be a constant compiled into the frontend.
"""

import app.core.auth as auth


def test_the_default_scope_is_what_the_ui_has_always_sent(monkeypatch):
    """Nobody who has not configured this should see a change."""
    monkeypatch.delenv("OIDC_SCOPE", raising=False)
    import importlib

    importlib.reload(auth)
    assert auth.OIDC_SCOPE == "openid profile email groups"


def test_a_peer_audience_can_be_requested(monkeypatch):
    monkeypatch.setenv(
        "OIDC_SCOPE", "openid profile email groups audience:server:client_id:harbor"
    )
    import importlib

    importlib.reload(auth)
    assert "audience:server:client_id:harbor" in auth.OIDC_SCOPE


def test_the_config_the_frontend_reads_carries_the_scope():
    """The frontend builds the authorize URL from this, so it must be served."""
    cfg = auth.AuthConfig(type="oidc")
    assert cfg.scope == "openid profile email groups"

    cfg = auth.AuthConfig(type="oidc", scope="openid audience:server:client_id:harbor")
    assert cfg.scope == "openid audience:server:client_id:harbor"
