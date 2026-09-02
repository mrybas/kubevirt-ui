"""CDI imports a registry source once. Re-pushing a tag updates no disk.

A publish that overwrites a tag looks successful and ships nothing, so it is
refused at publish time rather than discovered at boot time.
"""

import pytest

from app.core.image_publish import assert_tag_is_free


class _Harbor:
    def __init__(self, tags):
        self._tags = tags

    async def list_artifacts(self, token, project, repository):
        return [{"tags": [{"name": t} for t in self._tags]}]


async def test_a_free_tag_is_allowed():
    await assert_tag_is_free(_Harbor([]), "tok", "p", "u", "20260902")


async def test_an_occupied_tag_is_refused_by_name():
    with pytest.raises(ValueError) as exc:
        await assert_tag_is_free(_Harbor(["20260902"]), "tok", "p", "u", "20260902")

    assert "20260902" in str(exc.value)
