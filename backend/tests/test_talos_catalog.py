"""One catalogue, one `is_compatible()`, two readers that cannot disagree.

Before this there was a single hardcoded image URL: one Talos version for
everyone, and nothing at all saying which Kubernetes it takes. The dangerous
shape is not the hardcode though — it is what usually replaces it, a wizard
that computes compatibility on one side and a validator that computes it on
the other. This codebase has met that shape repeatedly and it always presents
the same way: the interface offers a combination the backend then refuses, or
worse, accepts.

So the anti-drift test below compares the *sets* — every pair the endpoint
publishes against every pair the validator lets through. It is parameterised
over the catalogue rather than over a copy of it, which is the U11 lesson:
assert the meaning, not a transcription of the current text.
"""

import json

import pytest
from fastapi import HTTPException

from app.core.talos_catalog import (
    CATALOG_ENV,
    BUILT_IN_CATALOG,
    TalosRelease,
    catalog,
    compatible_pairs,
    default_release,
    is_compatible,
)


class TestTheWindowIsComparedAsNumbers:
    @pytest.mark.parametrize("k8s,expected", [
        ("v1.31.0", True),
        ("1.32.1", True),
        ("v1.36.99", True),
        ("v1.30.1", False),      # below
        ("v1.37.0", False),      # above
    ])
    def test_inside_and_outside(self, k8s, expected) -> None:
        assert is_compatible(BUILT_IN_CATALOG[0], k8s) is expected

    def test_a_patch_release_does_not_narrow_the_window(self) -> None:
        """The window is stated in minors (1.31-1.36) and tenants ask in
        patches (1.32.1). Comparing the full string would reject every real
        request."""
        assert is_compatible(BUILT_IN_CATALOG[0], "v1.32.1")

    def test_nine_is_not_greater_than_thirty_one(self) -> None:
        """String comparison puts "1.9" above "1.31" and would accept a
        version six minors outside the window."""
        rel = TalosRelease(talos="x", image_url="u", k8s_min="1.31", k8s_max="1.36")

        assert is_compatible(rel, "v1.9.0") is False

    @pytest.mark.parametrize("junk", ["", "stable", "v", None])
    def test_unparseable_is_refused_not_assumed(self, junk) -> None:
        assert is_compatible(BUILT_IN_CATALOG[0], junk) is False


class TestTheCatalogueIsCurated:
    def test_the_built_in_matches_what_the_lab_runs(self) -> None:
        """It is the version the hardcoded URL pointed at; replacing a
        constant with a catalogue must not quietly change what gets built."""
        [only] = BUILT_IN_CATALOG

        assert only.talos == "1.13.8"
        assert "v1.13.8" in only.image_url
        assert "openstack-amd64.raw.xz" in only.image_url

    def test_the_image_is_the_openstack_variant(self, ) -> None:
        """`nocloud` looks for a `cidata` disk, CAPK attaches an OpenStack
        config-2 one, and the worker sits in maintenance mode forever — boots
        fine, never joins."""
        assert "nocloud" not in BUILT_IN_CATALOG[0].image_url

    def test_an_override_replaces_the_offering(self, monkeypatch) -> None:
        monkeypatch.setenv(CATALOG_ENV, json.dumps([
            {"talos": "1.14.0", "k8s_min": "1.32", "k8s_max": "1.37",
             "default": True},
        ]))

        assert [e.talos for e in catalog()] == ["1.14.0"]
        assert default_release().talos == "1.14.0"

    def test_an_override_may_omit_the_url_and_get_the_factory_one(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv(CATALOG_ENV, json.dumps([
            {"talos": "1.14.0", "k8s_min": "1.32", "k8s_max": "1.37"},
        ]))

        assert "v1.14.0" in catalog()[0].image_url

    def test_a_broken_override_falls_back_loudly(self, monkeypatch, caplog) -> None:
        """Refusing to serve any Talos at all because a JSON comma is wrong
        would be worse; but silence would leave the operator staring at a
        version that simply never appears."""
        monkeypatch.setenv(CATALOG_ENV, "{not json")

        with caplog.at_level("ERROR"):
            entries = catalog()

        assert [e.talos for e in entries] == ["1.13.8"]
        assert any("catalogue" in r.message.lower() or CATALOG_ENV in r.message
                   for r in caplog.records)

    def test_a_catalogue_with_no_default_still_resolves(self, monkeypatch) -> None:
        monkeypatch.setenv(CATALOG_ENV, json.dumps([
            {"talos": "1.14.0", "k8s_min": "1.32", "k8s_max": "1.37"},
        ]))

        assert default_release().talos == "1.14.0"


class TestTheValidator:
    def test_it_accepts_a_pair_inside_the_window(self) -> None:
        from app.api.v1.tenants_talos import resolve_talos_release

        assert resolve_talos_release("v1.32.1", None).talos == "1.13.8"

    def test_out_of_range_is_a_422_that_says_what_would_work(self) -> None:
        from app.api.v1.tenants_talos import resolve_talos_release

        with pytest.raises(HTTPException) as e:
            resolve_talos_release("v1.30.1", None)

        assert e.value.status_code == 422
        assert "1.31" in e.value.detail and "1.36" in e.value.detail

    def test_an_unknown_talos_version_lists_the_offered_ones(self) -> None:
        from app.api.v1.tenants_talos import resolve_talos_release

        with pytest.raises(HTTPException) as e:
            resolve_talos_release("v1.32.1", "9.9.9")

        assert e.value.status_code == 422
        assert "1.13.8" in e.value.detail

    def test_the_v_prefix_is_accepted_for_talos_too(self) -> None:
        from app.api.v1.tenants_talos import resolve_talos_release

        assert resolve_talos_release("v1.32.1", "v1.13.8").talos == "1.13.8"


class TestTheTwoReadersCannotDrift:
    """The point of the whole task."""

    @pytest.mark.asyncio
    async def test_every_published_pair_is_a_pair_the_validator_accepts(
        self,
    ) -> None:
        from unittest.mock import MagicMock

        from app.api.v1.tenants_crud import list_talos_versions
        from app.api.v1.tenants_talos import resolve_talos_release

        published = await list_talos_versions(kubernetes_version=None, user=MagicMock())

        for item in published["items"]:
            lo = int(item["k8s_min"].split(".")[1])
            hi = int(item["k8s_max"].split(".")[1])
            for minor in range(lo, hi + 1):
                # Must not raise: the wizard is offering this exact pair.
                resolve_talos_release(f"v1.{minor}.0", item["talos"])

    @pytest.mark.asyncio
    async def test_and_no_pair_outside_it_is_accepted(self) -> None:
        from unittest.mock import MagicMock

        from app.api.v1.tenants_crud import list_talos_versions
        from app.api.v1.tenants_talos import resolve_talos_release

        published = await list_talos_versions(kubernetes_version=None, user=MagicMock())

        for item in published["items"]:
            lo = int(item["k8s_min"].split(".")[1])
            hi = int(item["k8s_max"].split(".")[1])
            for minor in (lo - 1, hi + 1):
                with pytest.raises(HTTPException):
                    resolve_talos_release(f"v1.{minor}.0", item["talos"])

    @pytest.mark.asyncio
    async def test_the_sets_are_equal_under_a_different_catalogue(
        self, monkeypatch,
    ) -> None:
        """Parameterised over the catalogue, not over a copy of today's one —
        a test that hardcodes 1.13.8 would pass while the mechanism rotted."""
        from unittest.mock import MagicMock

        from app.api.v1.tenants_crud import list_talos_versions
        from app.api.v1.tenants_talos import resolve_talos_release

        monkeypatch.setenv(CATALOG_ENV, json.dumps([
            {"talos": "1.13.8", "k8s_min": "1.31", "k8s_max": "1.33"},
            {"talos": "1.14.1", "k8s_min": "1.34", "k8s_max": "1.36",
             "default": True},
        ]))

        published = {
            (i["talos"], f"1.{m}")
            for i in (await list_talos_versions(kubernetes_version=None, user=MagicMock()))["items"]
            for m in range(int(i["k8s_min"].split(".")[1]),
                           int(i["k8s_max"].split(".")[1]) + 1)
        }
        accepted = set()
        for talos, _ in compatible_pairs():
            for minor in range(28, 40):
                try:
                    resolve_talos_release(f"v1.{minor}.0", talos)
                except HTTPException:
                    continue
                accepted.add((talos, f"1.{minor}"))

        assert published == accepted
        assert ("1.14.1", "1.35") in published


class TestTheHardcodeIsGone:
    def test_the_golden_url_comes_from_the_catalogue(self, monkeypatch) -> None:
        """`tenants_talos.py:70` was one Talos version for every tenant. A
        constant left beside the catalogue would be a second source of truth."""
        from app.api.v1.tenants_talos import _default_golden_url

        monkeypatch.setenv(CATALOG_ENV, json.dumps([
            {"talos": "1.14.0", "k8s_min": "1.32", "k8s_max": "1.37",
             "default": True},
        ]))

        assert "v1.14.0" in _default_golden_url()

    def test_the_import_uses_the_resolved_release(self) -> None:
        from pathlib import Path

        src = Path("app/api/v1/tenants_crud.py").read_text()
        body = src[src.index("resolve_talos_release(req.kubernetes_version"):]
        body = body[:body.index(")", body.index("ensure_shared_golden_image"))]

        # T2 made the import shared, so the resolved release now supplies both
        # halves of the identity: which version to import, and from where.
        assert "release.image_url" in body
        assert "release.talos" in body


class TestTheEndpointFiltersSoTheWizardNeedNot:
    """T4's contract, enforced where it can be.

    The wizard renders what this returns and applies no rule of its own — the
    first cut of it filtered client-side by comparing minor versions, which is
    a second implementation of `is_compatible()` and exactly the shape that has
    cost this project repeatedly. Asking the server per Kubernetes version
    removes the copy rather than keeping the two in step.
    """

    @pytest.mark.asyncio
    async def test_it_returns_only_releases_that_take_this_kubernetes(self) -> None:
        from unittest.mock import MagicMock

        from app.api.v1.tenants_crud import list_talos_versions

        ok = await list_talos_versions(kubernetes_version="v1.33.5", user=MagicMock())
        assert [i["talos"] for i in ok["items"]] == ["1.13.8"]
        assert ok["hidden"] == 0

    @pytest.mark.asyncio
    async def test_an_unsupported_kubernetes_yields_nothing_and_says_how_many(
        self,
    ) -> None:
        """Empty with a count, not empty in silence: a version missing without
        explanation reads as a catalogue that lost it."""
        from unittest.mock import MagicMock

        from app.api.v1.tenants_crud import list_talos_versions

        out = await list_talos_versions(kubernetes_version="v1.30.1", user=MagicMock())

        assert out["items"] == []
        assert out["hidden"] == 1
        assert out["default"] == ""

    @pytest.mark.asyncio
    async def test_the_default_never_survives_the_filter_alone(
        self, monkeypatch,
    ) -> None:
        """Offering a preselected release the validator would refuse is the
        drift this endpoint exists to prevent."""
        import json
        from unittest.mock import MagicMock

        from app.api.v1.tenants_crud import list_talos_versions

        monkeypatch.setenv(CATALOG_ENV, json.dumps([
            {"talos": "1.13.8", "k8s_min": "1.31", "k8s_max": "1.33",
             "default": True},
            {"talos": "1.14.1", "k8s_min": "1.34", "k8s_max": "1.36"},
        ]))

        out = await list_talos_versions(kubernetes_version="v1.35.0", user=MagicMock())

        assert [i["talos"] for i in out["items"]] == ["1.14.1"]
        assert out["default"] == "1.14.1", "it offered a default it just filtered out"

    @pytest.mark.asyncio
    async def test_without_the_parameter_it_still_lists_everything(self) -> None:
        """Anything that wants the whole catalogue — a settings screen, a
        support dump — must not have to name a Kubernetes version."""
        from unittest.mock import MagicMock

        from app.api.v1.tenants_crud import list_talos_versions

        out = await list_talos_versions(kubernetes_version=None, user=MagicMock())

        assert out["items"] and out["hidden"] == 0

    @pytest.mark.asyncio
    async def test_what_it_offers_is_what_the_validator_accepts(self) -> None:
        """The anti-drift check, now at the level the wizard actually consumes."""
        from unittest.mock import MagicMock

        from app.api.v1.tenants_crud import list_talos_versions
        from app.api.v1.tenants_talos import resolve_talos_release

        for minor in range(29, 38):
            k8s = f"v1.{minor}.0"
            offered = await list_talos_versions(
                kubernetes_version=k8s, user=MagicMock())
            for item in offered["items"]:
                resolve_talos_release(k8s, item["talos"])   # must not raise
            if not offered["items"]:
                with pytest.raises(HTTPException):
                    resolve_talos_release(k8s, None)
