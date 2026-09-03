from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from _pytest.monkeypatch import MonkeyPatch
from pytest_mock import MockerFixture

from api.endpoints.ratings import report_lecturer
from api.schemas.user import User, UserInfo
from api.settings import settings


def user_info(user_id: str, name: str) -> UserInfo:
    return UserInfo(id=user_id, name=name, display_name=f"{name.title()} Person", avatar_url=None)


async def test__report_lecturer__mails_the_addresses_fetched_from_the_auth_service(
    mocker: MockerFixture, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "contact_email", "abuse@example.com")
    rating = MagicMock(
        lecturer_id="lecturer42",
        skill_id="skill42",
        webinar_name="Webinar",
        webinar_timestamp=datetime(2026, 9, 3, 12, 0, 0),
    )
    mocker.patch("api.models.LecturerRating.get_unrated", AsyncMock(return_value=rating))
    mocker.patch(
        "api.endpoints.ratings.get_userinfo",
        AsyncMock(side_effect=[user_info("student42", "student"), user_info("lecturer42", "lecturer")]),
    )
    get_email = mocker.patch(
        "api.endpoints.ratings.get_email", AsyncMock(side_effect=["student@example.com", "lecturer@example.com"])
    )
    send_email = mocker.patch("api.endpoints.ratings.send_email", AsyncMock())
    delete = mocker.patch("api.endpoints.ratings.db.delete", AsyncMock())

    result = await report_lecturer("rating42", "reason", User(id="student42", email_verified=True, admin=False))

    assert result is True
    assert [call.args for call in get_email.call_args_list] == [("student42",), ("lecturer42",)]
    recipient, title, body = send_email.call_args.args
    assert recipient == "abuse@example.com"
    assert title == "[Report] Student Person (student) reported Lecturer Person (lecturer)"
    assert "Student Person (student, student@example.com) reported" in body
    assert "Lecturer Person (lecturer, lecturer@example.com) for the webinar Webinar" in body
    assert send_email.call_args.kwargs == {"reply_to": "student@example.com"}
    delete.assert_called_once_with(rating)
