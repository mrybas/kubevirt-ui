"""Phase 2 — LDAP group autocomplete service.

Covers `app.core.ldap`:

* 3-tier fallback (external LDAP → bundled LLDAP → empty).
* RFC 4515 filter escape (no injection via the `q` query parameter).
* Substring matching against LLDAP `displayName`.
* Graceful degradation when neither backend is configured.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import ldap as ldap_mod


# ---------------------------------------------------------------------------
# RFC 4515 filter escape — `_escape_ldap_filter`
# ---------------------------------------------------------------------------

class TestEscapeLdapFilter:
    def test_plain_string_unchanged(self):
        assert ldap_mod._escape_ldap_filter("simple") == "simple"

    @pytest.mark.parametrize("ch,escaped", [
        ("*", "\\2a"),
        ("(", "\\28"),
        (")", "\\29"),
        ("\\", "\\5c"),
        ("\0", "\\00"),
    ])
    def test_special_chars_escaped(self, ch, escaped):
        assert ldap_mod._escape_ldap_filter(ch) == escaped

    def test_filter_injection_attempt_neutralised(self):
        # A naïve substitution would let `*)(uid=*` close the cn=*…* clause
        # and inject a new filter.  The escape must neutralise all three
        # metachars: *, (, ).
        evil = "*)(uid=admin)("
        escaped = ldap_mod._escape_ldap_filter(evil)
        assert "*" not in escaped
        assert "(" not in escaped
        assert ")" not in escaped
        # The escaped output still has the literal letters.
        assert "uid=admin" in escaped

    def test_control_chars_escaped(self):
        for code in (0x01, 0x07, 0x1f):
            ch = chr(code)
            assert ldap_mod._escape_ldap_filter(ch) == f"\\{code:02x}"

    def test_unicode_passes_through(self):
        # Non-control unicode is left alone (LDAP libraries handle UTF-8).
        assert ldap_mod._escape_ldap_filter("ünïçødé") == "ünïçødé"


# ---------------------------------------------------------------------------
# Backend detection — `is_external_ldap_configured`
# ---------------------------------------------------------------------------

class TestBackendDetection:
    def test_external_when_url_set(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "ldaps://ipa.example:636")
        assert ldap_mod.is_external_ldap_configured()

    def test_not_external_when_url_empty(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "")
        assert not ldap_mod.is_external_ldap_configured()


# ---------------------------------------------------------------------------
# 3-tier fallback: external → bundled LLDAP → empty
# ---------------------------------------------------------------------------

class TestSearchGroupsFallback:
    @pytest.mark.asyncio
    async def test_external_ldap_path_used_when_url_set(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "ldaps://ipa.example:636")
        monkeypatch.setattr(ldap_mod, "LDAP_GROUP_BASE_DN", "dc=example,dc=com")

        called = {}

        def fake_sync(query, limit):
            called["query"] = query
            called["limit"] = limit
            return ["g1", "g2"]

        monkeypatch.setattr(ldap_mod, "_search_groups_sync", fake_sync)
        result = await ldap_mod.search_groups("dev", limit=10)
        assert result == ["g1", "g2"]
        assert called == {"query": "dev", "limit": 10}

    @pytest.mark.asyncio
    async def test_lldap_path_used_when_no_external(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "")
        # Patch the lldap_client import (deferred inside the function).
        from app.core import lldap_client as lldap_mod
        monkeypatch.setattr(lldap_mod, "LLDAP_ENABLED", True)

        fake_client = MagicMock()
        fake_client.list_groups = AsyncMock(return_value=[
            {"displayName": "devs"},
            {"displayName": "ops"},
            {"displayName": "lldap_admin"},  # filtered out
        ])
        monkeypatch.setattr(lldap_mod, "get_lldap_client", lambda: fake_client)

        result = await ldap_mod.search_groups("", limit=10)
        assert "devs" in result
        assert "ops" in result
        assert "lldap_admin" not in result

    @pytest.mark.asyncio
    async def test_empty_when_no_backend(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "")
        from app.core import lldap_client as lldap_mod_
        monkeypatch.setattr(lldap_mod_, "LLDAP_ENABLED", False)
        assert await ldap_mod.search_groups("anything") == []

    @pytest.mark.asyncio
    async def test_zero_limit_returns_empty(self, monkeypatch):
        assert await ldap_mod.search_groups("foo", limit=0) == []

    @pytest.mark.asyncio
    async def test_limit_capped_at_100(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "ldaps://x")
        monkeypatch.setattr(ldap_mod, "LDAP_GROUP_BASE_DN", "dc=x")
        captured = {}

        def fake_sync(query, limit):
            captured["limit"] = limit
            return []

        monkeypatch.setattr(ldap_mod, "_search_groups_sync", fake_sync)
        await ldap_mod.search_groups("foo", limit=500)
        assert captured["limit"] == 100


# ---------------------------------------------------------------------------
# LLDAP substring filtering
# ---------------------------------------------------------------------------

class TestLldapSubstring:
    @pytest.mark.asyncio
    async def test_lldap_filters_by_substring_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "")
        from app.core import lldap_client as lldap_mod_
        monkeypatch.setattr(lldap_mod_, "LLDAP_ENABLED", True)

        client = MagicMock()
        client.list_groups = AsyncMock(return_value=[
            {"displayName": "team-Devs"},
            {"displayName": "team-Ops"},
            {"displayName": "audit"},
        ])
        monkeypatch.setattr(lldap_mod_, "get_lldap_client", lambda: client)

        result = await ldap_mod.search_groups("dev", limit=20)
        assert result == ["team-Devs"]  # case-insensitive match

    @pytest.mark.asyncio
    async def test_lldap_empty_when_listgroups_fails(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "")
        from app.core import lldap_client as lldap_mod_
        monkeypatch.setattr(lldap_mod_, "LLDAP_ENABLED", True)

        def _raise():
            raise RuntimeError("boom")

        monkeypatch.setattr(lldap_mod_, "get_lldap_client", _raise)
        assert await ldap_mod.search_groups("x") == []


# ---------------------------------------------------------------------------
# External LDAP sync path — exceptions degrade to empty
# ---------------------------------------------------------------------------

class TestExternalLdapErrors:
    def test_sync_returns_empty_when_basedn_missing(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "ldap://x")
        monkeypatch.setattr(ldap_mod, "LDAP_GROUP_BASE_DN", "")
        assert ldap_mod._search_groups_sync("foo", 10) == []

    def test_sync_returns_empty_on_connection_failure(self, monkeypatch):
        monkeypatch.setattr(ldap_mod, "LDAP_URL", "ldap://x")
        monkeypatch.setattr(ldap_mod, "LDAP_GROUP_BASE_DN", "dc=x")

        class _BoomServer:
            def __init__(self, *a, **kw):
                raise OSError("connection refused")

        fake_ldap3 = MagicMock()
        fake_ldap3.Server = _BoomServer
        with patch.dict("sys.modules", {"ldap3": fake_ldap3}):
            assert ldap_mod._search_groups_sync("foo", 10) == []
