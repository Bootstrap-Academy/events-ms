from sqlalchemy import BigInteger, Column, String
from sqlalchemy.orm import Mapped

from api.database import Base


class Coaching(Base):
    __tablename__ = "events_coachings"

    user_id: Mapped[str] = Column(String(36), primary_key=True)
    skill_id: Mapped[str] = Column(String(256), primary_key=True)
    price: Mapped[int] = Column(BigInteger)
