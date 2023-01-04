import secrets
from typing import Literal

from pydantic import BaseSettings, Field


class Settings(BaseSettings):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000
    root_path: str = ""

    debug: bool = False
    reload: bool = False

    cache_ttl: int = 300

    jwt_secret: str = secrets.token_urlsafe(64)

    auth_url: str = ""
    skills_url: str = ""
    shop_url: str = ""

    webinar_level: int = 15
    coaching_level: int = 42

    rating_half_life: int = 60  # days
    rating_max_keep: int = 730  # days = 2 years

    webinar_participant_xp: int = 100
    webinar_lecturer_xp: int = 100

    coaching_participant_xp: int = 100
    coaching_lecturer_xp: int = 100

    event_fee: float = 0.3

    calendar_secret: str = secrets.token_urlsafe(64)
    webinar_registration_url: str = ""
    event_cancel_url: str = ""

    public_base_url: str = "http://localhost:8000"

    internal_jwt_ttl: int = 10

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = False
    smtp_starttls: bool = True

    contact_email: str | None = None

    database_url: str = Field(
        "mysql+aiomysql://fastapi:fastapi@mariadb:3306/fastapi",
        regex=r"^(mysql\+aiomysql|postgresql\+asyncpg|sqlite\+aiosqlite)://.*$",
    )
    pool_recycle: int = 300
    pool_size: int = 20
    max_overflow: int = 20
    sql_show_statements: bool = False

    redis_url: str = Field("redis://redis:6379/4", regex=r"^redis://.*$")
    auth_redis_url: str = Field("redis://redis:6379/0", regex=r"^redis://.*$")

    sentry_dsn: str | None = None
    sentry_environment: str = "test"


settings = Settings()
