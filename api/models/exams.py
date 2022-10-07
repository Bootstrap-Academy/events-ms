from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped

from api.database import Base


class Exam(Base):
    __tablename__ = "events_exams"

    user_id: Mapped[str] = Column(String(36), primary_key=True)
    skill_id: Mapped[str] = Column(String(256), primary_key=True)
