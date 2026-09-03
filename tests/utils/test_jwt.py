import re
from datetime import datetime, timedelta
from typing import Any, cast

import jwt as _jwt
import pytest
from _pytest.monkeypatch import MonkeyPatch
from pytest_mock import MockerFixture

from api.settings import settings
from api.utils import jwt


@pytest.mark.parametrize(
    "data,now,ttl,expected",
    [
        ({}, 42, 10, {"exp": 52}),
        ({"foo": "bar", "x": 42, "y": 1337}, 123, 456, {"foo": "bar", "x": 42, "y": 1337, "exp": 579}),
        ({"foo": {"bar": [{"x": 42}, {"y": 1337}]}}, 123, 456, {"foo": {"bar": [{"x": 42}, {"y": 1337}]}, "exp": 579}),
    ],
)
async def test__jwt_encode(
    data: dict[str, Any], now: int, ttl: int, expected: dict[str, Any], mocker: MockerFixture, monkeypatch: MonkeyPatch
) -> None:
    mocker.patch("api.utils.jwt.utcnow", lambda: datetime.utcfromtimestamp(now))
    monkeypatch.setattr(settings, "jwt_secret", "My JWT secret")

    token = jwt.encode_jwt(data, timedelta(seconds=ttl))

    match = re.match(r"^([a-zA-Z\d\-_]+)\.([a-zA-Z\d\-_]+)\.[a-zA-Z\d\-_]+$", token)
    assert match, "Invalid JWT format"

    assert _jwt.get_unverified_header(token) == {"typ": "JWT", "alg": "HS256"}
    assert _jwt.decode(token, "My JWT secret", ["HS256"], {"verify_exp": False}) == expected


@pytest.mark.parametrize(
    "data,ttl,require,expected",
    [
        ({}, 1, [], True),
        ({}, -1, [], False),
        ({}, 1, ["foo"], False),
        ({"foo": "bar"}, 1, [], True),
        ({"foo": "bar"}, 1, ["foo"], True),
        ({"foo": "bar"}, 1, ["foo", "bar"], False),
        ({"foo": "bar"}, -1, ["foo"], False),
    ],
)
async def test__jwt_decode(
    data: dict[str, Any], ttl: int, require: list[str], expected: bool, monkeypatch: MonkeyPatch
) -> None:
    exp = (datetime.utcnow() + timedelta(seconds=ttl)).replace(microsecond=0)
    token = _jwt.encode(data | {"exp": exp}, "My JWT secret", "HS256")
    monkeypatch.setattr(settings, "jwt_secret", "My JWT secret")
    result = jwt.decode_jwt(token, require=require)

    if expected:
        assert isinstance(result, dict)
        exp_ = cast(dict[str, Any], result).pop("exp")
        assert result == data
        assert exp == datetime.utcfromtimestamp(exp_)
    else:
        assert result is None


async def test__jwt_encode__explicit_secret(mocker: MockerFixture, monkeypatch: MonkeyPatch) -> None:
    mocker.patch("api.utils.jwt.utcnow", lambda: datetime.utcfromtimestamp(42))
    monkeypatch.setattr(settings, "jwt_secret", "the shared secret")

    token = jwt.encode_jwt({"aud": "events"}, timedelta(seconds=10), secret="the events secret")

    assert _jwt.decode(token, "the events secret", ["HS256"], {"verify_exp": False, "verify_aud": False}) == {
        "aud": "events",
        "exp": 52,
    }
    with pytest.raises(_jwt.InvalidSignatureError):
        _jwt.decode(token, "the shared secret", ["HS256"], {"verify_exp": False, "verify_aud": False})


async def test__jwt_decode__explicit_secret(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "the shared secret")
    exp = (datetime.utcnow() + timedelta(seconds=10)).replace(microsecond=0)
    token = _jwt.encode({"aud": "events", "exp": exp}, "the events secret", "HS256")

    assert jwt.decode_jwt(token, audience=["events"], secret="the events secret") is not None
    assert jwt.decode_jwt(token, audience=["events"]) is None
