"""A registry outage must not stop anyone booting a VM from a disk they have.

This is the reason the catalogue is not the source of truth for the list.
"""

import pytest

from app.api.v1.images_catalog import catalog_images
from app.core.harbor_client import HarborUnauthorized, HarborUnavailable


class _Down:
    async def verify_identity(self, token):
        raise HarborUnavailable("no route to host")

    async def list_projects(self, token):
        raise HarborUnavailable("no route to host")


class _Rejecting:
    async def verify_identity(self, token):
        raise HarborUnauthorized("token expired")

    async def list_projects(self, token):
        raise AssertionError(
            "list_projects must not run once verify_identity has rejected the token"
        )


async def test_an_unreachable_harbor_raises_rather_than_returning_nothing():
    """The caller must be able to tell 'no images' from 'could not ask'."""
    with pytest.raises(HarborUnavailable):
        await catalog_images(_Down(), "tok")


async def test_a_rejected_token_stays_distinguishable_from_an_outage():
    with pytest.raises(HarborUnauthorized):
        await catalog_images(_Rejecting(), "tok")
