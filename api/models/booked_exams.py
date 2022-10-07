from sqlalchemy import Boolean, Column, String
from sqlalchemy.orm import Mapped

from api.database import Base


class BookedExam(Base):
    __tablename__ = "events_booked_exams"

    user_id: Mapped[str] = Column(String(36), primary_key=True)
    skill_id: Mapped[str] = Column(String(256), primary_key=True)
    examiner_id: Mapped[str] = Column(String(36))
    confirmed: Mapped[bool] = Column(Boolean)
    calendly_link: Mapped[str] = Column(String(256))
