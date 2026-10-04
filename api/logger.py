import logging
import sys

import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import DefaultFormatter

from .settings import settings
from .telemetry import sanitize_sentry_breadcrumb, sanitize_sentry_event


def setup_sentry(app: FastAPI, dsn: str, name: str, version: str) -> None:
    """Initialize sentry connection."""

    sentry_sdk.init(
        dsn=dsn,
        attach_stacktrace=True,
        include_local_variables=False,
        max_request_body_size="never",
        send_default_pii=False,
        shutdown_timeout=5,
        integrations=[
            AioHttpIntegration(),
            FastApiIntegration(transaction_style="url"),
            SqlalchemyIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.WARNING),
        ],
        release=f"{name}@{version}",
        environment=settings.sentry_environment,
        before_send=sanitize_sentry_event,
        before_send_transaction=sanitize_sentry_event,
        before_breadcrumb=sanitize_sentry_breadcrumb,
    )
    ignore_logger("uvicorn.error")


logging_formatter = DefaultFormatter(fmt := "[%(asctime)s] %(levelprefix)s %(message)s")
LOGGING_CONFIG["formatters"]["default"]["fmt"] = fmt
LOGGING_CONFIG["formatters"]["access"][
    "fmt"
] = '[%(asctime)s] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'

logging_handler = logging.StreamHandler(sys.stdout)
logging_handler.setFormatter(logging_formatter)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with a given name."""

    logger: logging.Logger = logging.getLogger(name)
    logger.addHandler(logging_handler)
    logger.setLevel(settings.log_level)

    return logger
