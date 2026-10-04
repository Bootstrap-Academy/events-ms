from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from api.schemas.user import UserDetails, UserInfo
from api.services import auth


AUTH_SERVICE_RESPONSE = {
    "id": "user42",
    "name": "nickname",
    "display_name": "Display Name",
    "email": "user@example.com",
    "email_verified": True,
    "avatar_url": None,
    "admin": False,
}


def mock_auth_service(mocker: MockerFixture, status_code: int) -> MagicMock:
    internal_service = mocker.patch("api.services.auth.InternalService")
    client = cast(MagicMock, internal_service.AUTH.client.__aenter__.return_value)
    client.get = AsyncMock(
        return_value=MagicMock(status_code=status_code, json=MagicMock(return_value=AUTH_SERVICE_RESPONSE))
    )
    return client


async def test___fetch_userinfo__does_not_cache_the_email_address(mocker: MockerFixture) -> None:
    client = mock_auth_service(mocker, 200)

    result = await auth._fetch_userinfo.__wrapped__("user42")  # type: ignore

    client.get.assert_called_once_with("/users/user42")
    assert result == {"id": "user42", "display_name": "Display Name", "avatar_url": None}


async def test___fetch_userinfo__unknown_user(mocker: MockerFixture) -> None:
    mock_auth_service(mocker, 404)

    assert await auth._fetch_userinfo.__wrapped__("user42") is None  # type: ignore


@pytest.mark.parametrize("data", [None, {"id": "user42", "name": "nickname", "display_name": "D", "avatar_url": None}])
async def test__get_userinfo(mocker: MockerFixture, data: dict[str, str] | None) -> None:
    fetch_userinfo = mocker.patch("api.services.auth._fetch_userinfo", AsyncMock(return_value=data))

    result = await auth.get_userinfo("user42")

    fetch_userinfo.assert_called_once_with("user42")
    assert result == (UserInfo(**data) if data else None)
    if result is not None:
        # Warm caches from older versions must not leak either account name.
        assert "name" not in result.dict()
        assert "display_name" not in result.dict()


async def test_internal_contract_details_keep_the_display_name(mocker: MockerFixture) -> None:
    mocker.patch("api.services.auth._fetch_userinfo", AsyncMock(return_value=AUTH_SERVICE_RESPONSE))

    result = await auth.get_userdetails("user42")

    assert result == UserDetails(id="user42", display_name="Display Name", avatar_url=None)
    assert str(result) == "Display Name"
    assert "name" not in result.dict()
    assert "email" not in result.dict()
