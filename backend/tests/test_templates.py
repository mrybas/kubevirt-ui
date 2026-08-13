"""VM templates: names are cluster-wide, projects are not."""

import pytest

@pytest.mark.asyncio
async def test_duplicate_template_names_where_the_old_one_points():
    """A name collision is usually with a template the user cannot see.

    Templates live by name in one cluster-wide ConfigMap while the wizard only
    offers those whose image is in the selected project. Creating
    `ubuntu-small` in `acme-dev` collided with a leftover of the same name
    pointing at the deleted namespace `e2e-lab-prod` — invisible in the list,
    and the message said only "already exists".
    """
    import json
    from unittest.mock import AsyncMock, MagicMock
    from fastapi import HTTPException

    from app.api.v1.templates import create_template
    from app.models.template import VMTemplateCreate

    existing = {
        "display_name": "Ubuntu Small",
        "golden_image_name": "ubuntu-22-04-75x54",
        "golden_image_namespace": "e2e-lab-prod",
    }
    cm = MagicMock()
    cm.data = {"ubuntu-small": json.dumps(existing)}

    k8s = MagicMock()
    k8s.core_api.read_namespace = AsyncMock()
    k8s.core_api.read_namespaced_config_map = AsyncMock(return_value=cm)

    request = MagicMock()
    request.app.state.k8s_client = k8s

    body = VMTemplateCreate(
        name="ubuntu-small", display_name="Ubuntu Small",
        golden_image_name="ubuntu-22-04-m25bz", golden_image_namespace="acme-dev",
    )

    with pytest.raises(HTTPException) as e:
        await create_template(body, request, user=MagicMock())

    assert e.value.status_code == 409
    assert "e2e-lab-prod" in e.value.detail, "must say where the existing one points"
    assert "ubuntu-22-04-75x54" in e.value.detail
    assert "delete that template" in e.value.detail
