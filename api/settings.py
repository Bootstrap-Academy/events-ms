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

    # Secrets for the internal service tokens, one per audience. An empty value
    # falls back to `jwt_secret`, so a deployment which has not rolled out the
    # per-audience secrets yet keeps working.
    internal_jwt_secret_auth: str = ""
    internal_jwt_secret_shop: str = ""
    internal_jwt_secret_skills: str = ""
    internal_jwt_secret_events: str = ""

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

    webinar_registration_url: str = ""
    event_cancel_url: str = ""

    event_url: str = ""

    public_base_url: str = "http://localhost:8000"

    internal_jwt_ttl: int = 10

    deleted_user_sweep_batch_size: int = 500
    deleted_user_sweep_rate_limit: float = 10  # auth service requests per second

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

    def internal_jwt_secret(self, audience: str) -> str:
        """Return the secret with which internal tokens for `audience` are signed and verified."""

        secrets_by_audience = {
            "auth": self.internal_jwt_secret_auth,
            "shop": self.internal_jwt_secret_shop,
            "skills": self.internal_jwt_secret_skills,
            "events": self.internal_jwt_secret_events,
        }
        return secrets_by_audience.get(audience, "") or self.jwt_secret


settings = Settings()  # type: ignore
