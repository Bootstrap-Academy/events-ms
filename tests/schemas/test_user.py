from typing import Any

from api.schemas.ratings import Unrated
from api.schemas.user import UserInfo


AUTH_SERVICE_RESPONSE: dict[str, Any] = {
    "id": "user42",
    "name": "nickname",
    "display_name": "Display Name",
    "email": "user@example.com",
    "email_verified": True,
    "avatar_url": None,
    "admin": False,
    "last_login": 1234567890,
}


def test__user_info__does_not_declare_an_email_field() -> None:
    assert "email" not in UserInfo.__fields__
    assert "email" not in UserInfo.schema()["properties"]


def test__user_info__drops_the_email_address_of_the_auth_service() -> None:
    user_info = UserInfo(**AUTH_SERVICE_RESPONSE)

    assert user_info.dict() == {"id": "user42", "name": "nickname", "display_name": "Display Name", "avatar_url": None}
    assert not hasattr(user_info, "email")


def test__user_info__is_not_serialized_with_an_email_address() -> None:
    unrated = Unrated(
        id="rating42",
        instructor=UserInfo(**AUTH_SERVICE_RESPONSE),
        skill_id="skill42",
        webinar_timestamp=1234567890,
        webinar_name="Webinar",
    )

    assert "email" not in unrated.dict()["instructor"]
