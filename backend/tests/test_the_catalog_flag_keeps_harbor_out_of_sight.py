"""With HARBOR_IMAGE_ENABLED unset, nothing about Harbor may be observable."""

import app.core.operator as operator


def test_the_harbor_path_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("HARBOR_IMAGE_ENABLED", raising=False)
    assert operator.harbor_image_path_enabled() is False


def test_the_harbor_path_turns_on_with_the_same_truthy_words_as_its_siblings(monkeypatch):
    for word in ("true", "1", "yes"):
        monkeypatch.setenv("HARBOR_IMAGE_ENABLED", word)
        assert operator.harbor_image_path_enabled() is True, word


def test_an_image_defaults_to_being_a_cluster_image():
    from app.models.template import VMImage

    img = VMImage(name="ubuntu", namespace="default", status="Ready")

    assert img.origin == "cluster"
    assert img.catalog_ref is None


def test_a_list_claims_the_catalog_is_present_unless_told_otherwise():
    from app.models.template import VMImageListResponse

    assert VMImageListResponse(items=[], total=0).catalog_available is True
