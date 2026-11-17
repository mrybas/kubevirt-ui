"""An OIDC token authenticates only when it was issued to this client.

Validation asked the issuer's userinfo endpoint and nothing else. That endpoint
serves every client of the issuer, so an access token minted for any other
application there logged in here as well, past whatever login policy the
issuer applies to this application.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

from app.core import auth


def _token(**claims: Any) -> str:
    return jwt.encode({"sub": "u", **claims}, "irrelevant", algorithm="HS256")


async def _validate(token: str, userinfo_status: int = 200) -> auth.User | None:
    response = MagicMock(status_code=userinfo_status)
    response.json.return_value = {"sub": "u", "email": "u@example.test", "groups": []}
    http = MagicMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    http.get = AsyncMock(return_value=response)
    with (
        patch.object(auth, "OIDC_CLIENT_ID", "kubevirt-ui"),
        patch.object(auth, "get_oidc_config", AsyncMock(
            return_value={"userinfo_endpoint": "https://idp.example.test/userinfo"},
        )),
        patch.object(auth.httpx, "AsyncClient", return_value=http),
    ):
        return await auth.validate_oidc_token(token)


@pytest.mark.asyncio
class TestWhoTheTokenWasFor:
    async def test_a_token_for_another_app_is_refused(self) -> None:
        assert await _validate(_token(aud="grafana", azp="grafana")) is None

    async def test_a_token_for_this_app_is_accepted(self) -> None:
        user = await _validate(_token(aud="kubevirt-ui"))
        assert user is not None and user.email == "u@example.test"

    async def test_this_app_among_several_audiences_is_accepted(self) -> None:
        assert await _validate(_token(aud=["peer", "kubevirt-ui"])) is not None

    async def test_a_cross_client_token_requested_by_this_app_is_accepted(self) -> None:
        """Dex with `audience:server:client_id:<peer>` puts the peer in `aud`
        and the requesting client in `azp`."""
        assert await _validate(_token(aud=["peer"], azp="kubevirt-ui")) is not None

    async def test_an_opaque_token_is_refused(self) -> None:
        assert await _validate("not-a-jwt") is None

    async def test_userinfo_still_has_the_last_word(self) -> None:
        assert await _validate(_token(aud="kubevirt-ui"), userinfo_status=401) is None
