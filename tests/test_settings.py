from _pytest.monkeypatch import MonkeyPatch

from api.settings import settings


def test__internal_jwt_secret__falls_back_to_the_shared_secret(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "shared")
    monkeypatch.setattr(settings, "internal_jwt_secret_auth", "")
    monkeypatch.setattr(settings, "internal_jwt_secret_shop", "")
    monkeypatch.setattr(settings, "internal_jwt_secret_skills", "")
    monkeypatch.setattr(settings, "internal_jwt_secret_events", "")

    assert settings.internal_jwt_secret("auth") == "shared"
    assert settings.internal_jwt_secret("shop") == "shared"
    assert settings.internal_jwt_secret("skills") == "shared"
    assert settings.internal_jwt_secret("events") == "shared"
    assert settings.internal_jwt_secret("unknown") == "shared"


def test__internal_jwt_secret__per_audience(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "shared")
    monkeypatch.setattr(settings, "internal_jwt_secret_auth", "auth secret")
    monkeypatch.setattr(settings, "internal_jwt_secret_shop", "shop secret")
    monkeypatch.setattr(settings, "internal_jwt_secret_skills", "skills secret")
    monkeypatch.setattr(settings, "internal_jwt_secret_events", "events secret")

    assert settings.internal_jwt_secret("auth") == "auth secret"
    assert settings.internal_jwt_secret("shop") == "shop secret"
    assert settings.internal_jwt_secret("skills") == "skills secret"
    assert settings.internal_jwt_secret("events") == "events secret"
    assert settings.internal_jwt_secret("unknown") == "shared"
