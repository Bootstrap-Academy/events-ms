from __future__ import annotations

from secrets import token_urlsafe

from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped

from api.database import Base


class CalendlyLink(Base):
    __tablename__ = "events_calendly_links"

    user_id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    api_token: Mapped[str] = Column(String(512))
    uri: Mapped[str] = Column(String(256))
    webhook_signing_key: Mapped[str] = Column(String(128))

    @classmethod
    def new(cls, user_id: str, api_token: str) -> CalendlyLink:
        return cls(user_id=user_id, api_token=api_token, webhook_signing_key=token_urlsafe(96))
