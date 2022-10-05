from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped

from api.database import Base


class CalendlyLink(Base):
    __tablename__ = "events_calendly_links"

    user_id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    api_token: Mapped[str] = Column(String(512))
    uri: Mapped[str] = Column(String(256))
