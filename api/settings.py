import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseSettings, Field


class Settings(BaseSettings):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000
    root_path: str = ""

    debug: bool = False
    reload: bool = False

    jwt_secret: str = secrets.token_urlsafe(64)

    auth_url: str = ""
    skills_url: str = ""
    shop_url: str = ""

    webinar_skill: str = "instructor"
    coaching_skill: str = "instructor"
    examiner_skill: str = "examiner"

    exam_price: int = 15000  # 150€ = 15000 MC
    exam_xp: int = 500

    public_base_url: str = "http://localhost:8000"

    calendly_cache_ttl: int = 3600  # 1 hour

    internal_jwt_ttl: int = 10

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


settings = Settings()
