from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base
from api.models.webinars import Webinar


class WebinarParticipant(Base):
    __tablename__ = "events_webinar_participants"

    webinar_id: Mapped[str] = Column(String(36), ForeignKey("events_webinars.id"), primary_key=True)
    webinar: Webinar = relationship("Webinar", back_populates="participants", lazy="selectin")
    user_id: Mapped[str] = Column(String(36), primary_key=True)
