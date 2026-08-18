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

        published = await list_talos_versions(user=MagicMock())

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

        published = await list_talos_versions(user=MagicMock())

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
            for i in (await list_talos_versions(user=MagicMock()))["items"]
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
        body = body[:body.index(")", body.index("ensure_talos_golden_image"))]

        assert "release.image_url" in body
