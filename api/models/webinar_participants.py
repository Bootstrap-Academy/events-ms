from sqlalchemy import BigInteger, Column, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base
from api.models.webinars import Webinar


class WebinarParticipant(Base):
    __tablename__ = "events_webinar_participants"

    webinar_id: Mapped[str] = Column(String(36), ForeignKey("events_webinars.id"), primary_key=True)
    webinar: Webinar = relationship("Webinar", back_populates="participants", lazy="selectin")
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    # What the participant was charged for this registration. It is not always the price of the webinar: a
    # registration that an emergency cancellation of the lecturer made free costs nothing, and the lecturer may
    # change the price afterwards. Every refund and the lecturer's share are computed from this amount, so that
    # nothing can be paid out for a booking that was never paid for.
    paid_coins: Mapped[int] = Column(BigInteger, nullable=False)
