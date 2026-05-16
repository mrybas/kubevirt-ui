"""Unit tests for app.core.naming."""

import pytest

from app.core.naming import (
    DISPLAY_NAME_ANNOTATION,
    DNS_1123_MAX,
    K8S_GENERATENAME_SUFFIX_LEN,
    SLUG_LABEL,
    SLUG_MAX,
    generate_k8s_name,
    get_display_name,
    sanitize_display_name,
    slugify,
    with_synthetic_metadata,
)


class TestSanitizeDisplayName:
    """Tests for sanitize_display_name (and its alias slugify)."""

    @pytest.mark.parametrize(
        "input_,expected",
        [
            ("Test VM 1", "test-vm-1"),
            ("Ubuntu 24.04 Server", "ubuntu-24-04-server"),
            ("My   Weird---Name!!!", "my-weird-name"),
            ("Web Server (prod)", "web-server-prod"),
            ("   leading whitespace", "leading-whitespace"),
            ("trailing whitespace   ", "trailing-whitespace"),
            ("---only-dashes---", "only-dashes"),
            ("UPPERCASE", "uppercase"),
            ("mixed_CASE-with.dots", "mixed-case-with-dots"),
            ("123abc", "123abc"),  # digit-leading OK for DNS-1123 subdomain
            ("a", "a"),  # single char
            ("0", "0"),
        ],
    )
    def test_basic_sanitization(self, input_: str, expected: str) -> None:
        assert sanitize_display_name(input_) == expected

    @pytest.mark.parametrize(
        "input_",
        [
            "",  # empty
            "!!!",  # all symbols
            "@@@@@",
            "  ",  # whitespace only
            "---",  # dashes only
            "...",
        ],
    )
    def test_fallback_to_unnamed(self, input_: str) -> None:
        assert sanitize_display_name(input_) == "unnamed"

    def test_max_length_truncation(self) -> None:
        long = "a" * 200
        result = sanitize_display_name(long)
        assert len(result) <= SLUG_MAX
        assert result == "a" * SLUG_MAX

    def test_truncation_strips_trailing_dash(self) -> None:
        # A 60-char input where truncation lands on a dash
        input_ = "a" * 56 + "-" + "b" * 10  # truncated at 57 → "aaaa...-"
        result = sanitize_display_name(input_)
        assert not result.endswith("-")
        assert len(result) <= SLUG_MAX

    def test_custom_max_length(self) -> None:
        assert sanitize_display_name("hello world", max_length=5) == "hello"

    def test_slugify_alias(self) -> None:
        # Backwards-compat alias
        assert slugify("Hello World") == "hello-world"

    def test_unicode_falls_back(self) -> None:
        # Non-ASCII gets replaced with hyphens, may collapse to "unnamed"
        assert sanitize_display_name("мояВМ") == "unnamed"
        assert sanitize_display_name("test-мояВМ") == "test"

    def test_slug_fits_within_dns_1123(self) -> None:
        # SLUG_MAX + 1 (dash) + 5 (k8s suffix) must fit in DNS-1123 (63)
        assert SLUG_MAX + 1 + K8S_GENERATENAME_SUFFIX_LEN == DNS_1123_MAX


class TestWithSyntheticMetadata:
    """Tests for with_synthetic_metadata helper."""

    def test_injects_generate_name(self) -> None:
        body = {"apiVersion": "v1", "kind": "ConfigMap", "spec": {}}
        with_synthetic_metadata(body, "My Config")
        assert body["metadata"]["generateName"] == "my-config-"

    def test_drops_existing_name(self) -> None:
        body = {"metadata": {"name": "old-name"}}
        with_synthetic_metadata(body, "New Display")
        assert "name" not in body["metadata"]
        assert body["metadata"]["generateName"] == "new-display-"

    def test_sets_display_name_annotation(self) -> None:
        body: dict = {}
        with_synthetic_metadata(body, "Web Server (prod)")
        assert body["metadata"]["annotations"][DISPLAY_NAME_ANNOTATION] == "Web Server (prod)"

    def test_sets_slug_label(self) -> None:
        body: dict = {}
        with_synthetic_metadata(body, "Web Server (prod)")
        assert body["metadata"]["labels"][SLUG_LABEL] == "web-server-prod"

    def test_preserves_existing_annotations(self) -> None:
        body = {"metadata": {"annotations": {"custom/key": "value"}}}
        with_synthetic_metadata(body, "Test")
        assert body["metadata"]["annotations"]["custom/key"] == "value"
        assert body["metadata"]["annotations"][DISPLAY_NAME_ANNOTATION] == "Test"

    def test_preserves_existing_labels(self) -> None:
        body = {"metadata": {"labels": {"app": "myapp"}}}
        with_synthetic_metadata(body, "Test")
        assert body["metadata"]["labels"]["app"] == "myapp"
        assert body["metadata"]["labels"][SLUG_LABEL] == "test"

    def test_namespace_added_when_provided(self) -> None:
        body: dict = {}
        with_synthetic_metadata(body, "Test", namespace="my-ns")
        assert body["metadata"]["namespace"] == "my-ns"

    def test_namespace_not_added_when_omitted(self) -> None:
        body: dict = {}
        with_synthetic_metadata(body, "Test")
        assert "namespace" not in body["metadata"]

    def test_returns_same_body(self) -> None:
        body: dict = {"apiVersion": "v1"}
        result = with_synthetic_metadata(body, "Test")
        assert result is body  # mutated in place, returned for chaining

    def test_generate_name_fits_dns_1123(self) -> None:
        long_display = "a" * 200
        body: dict = {}
        with_synthetic_metadata(body, long_display)
        # generateName = slug + "-", after K8s appends 5 chars, total <= 63
        assert len(body["metadata"]["generateName"]) + K8S_GENERATENAME_SUFFIX_LEN <= DNS_1123_MAX

    def test_unnamed_fallback(self) -> None:
        body: dict = {}
        with_synthetic_metadata(body, "")
        assert body["metadata"]["generateName"] == "unnamed-"
        assert body["metadata"]["annotations"][DISPLAY_NAME_ANNOTATION] == ""
        assert body["metadata"]["labels"][SLUG_LABEL] == "unnamed"


class TestGenerateK8sName:
    """Tests for the deprecated generate_k8s_name (local generation)."""

    def test_format(self) -> None:
        name = generate_k8s_name("Test VM")
        # slug + "-" + 6 hex chars
        assert name.startswith("test-vm-")
        suffix = name[len("test-vm-") :]
        assert len(suffix) == 6
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_uniqueness(self) -> None:
        # Two calls with same display name should produce different K8s names
        names = {generate_k8s_name("Test") for _ in range(100)}
        assert len(names) == 100  # all unique with overwhelming probability

    def test_empty_display(self) -> None:
        name = generate_k8s_name("")
        assert name.startswith("unnamed-")


class TestGetDisplayName:
    """Tests for get_display_name."""

    def test_reads_from_annotation(self) -> None:
        metadata = {
            "name": "test-q4r8z",
            "annotations": {DISPLAY_NAME_ANNOTATION: "My Test"},
        }
        assert get_display_name(metadata) == "My Test"

    def test_falls_back_to_name(self) -> None:
        metadata = {"name": "manual-resource"}
        assert get_display_name(metadata) == "manual-resource"

    def test_no_fallback_returns_none(self) -> None:
        metadata = {"name": "manual-resource"}
        assert get_display_name(metadata, fallback_to_name=False) is None

    def test_empty_annotation_falls_back(self) -> None:
        # Empty annotation value should still fall through to name
        metadata = {
            "name": "test-q4r8z",
            "annotations": {DISPLAY_NAME_ANNOTATION: ""},
        }
        assert get_display_name(metadata) == "test-q4r8z"

    def test_missing_annotations_dict(self) -> None:
        # metadata without "annotations" key at all
        metadata = {"name": "foo"}
        assert get_display_name(metadata) == "foo"

    def test_none_annotations_dict(self) -> None:
        # annotations: None — defensive handling
        metadata = {"name": "foo", "annotations": None}
        assert get_display_name(metadata) == "foo"

    def test_other_annotations_preserved(self) -> None:
        metadata = {
            "name": "x",
            "annotations": {"unrelated": "value", DISPLAY_NAME_ANNOTATION: "Display"},
        }
        assert get_display_name(metadata) == "Display"
