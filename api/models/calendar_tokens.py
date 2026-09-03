import secrets
from datetime import datetime

from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped

from ..database.database import UTCDateTime
from ..utils.utc import utcnow
from api.database import Base, db


# number of random bytes a calendar token is made of
TOKEN_BYTES = 32


class CalendarToken(Base):
    """Random per user token which grants read access to the ics calendar feed of that user."""

    __tablename__ = "events_calendar_tokens"

    user_id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    token: Mapped[str] = Column(String(64), unique=True)
    created_at: Mapped[datetime] = Column(UTCDateTime)

    @classmethod
    async def get_or_create(cls, user_id: str) -> "CalendarToken":
        """Return the calendar token of a user, creating one if they do not have one yet."""

        if token := await db.get(cls, user_id=user_id):
            return token
        return await db.add(cls(user_id=user_id, token=secrets.token_urlsafe(TOKEN_BYTES), created_at=utcnow()))

    @classmethod
    async def rotate(cls, user_id: str) -> "CalendarToken":
        """Issue a new calendar token for a user, which revokes the previous one."""

        token = await db.get(cls, user_id=user_id)
        if token is None:
            return await cls.get_or_create(user_id)

        token.token = secrets.token_urlsafe(TOKEN_BYTES)
        token.created_at = utcnow()
        return token

    @classmethod
    async def get_user_id(cls, token: str) -> str | None:
        """Return the user the given calendar token belongs to, or None if the token is unknown."""

        row: CalendarToken | None = await db.get(cls, token=token)
        return row.user_id if row else None
