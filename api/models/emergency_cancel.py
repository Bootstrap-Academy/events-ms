from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped

from api.database import Base, db, filter_by


class EmergencyCancel(Base):
    __tablename__ = "events_emergency_cancel"

    user_id: Mapped[str] = Column(String(36), primary_key=True, unique=True)

    @classmethod
    async def exists(cls, user_id: str) -> bool:
        return await db.exists(filter_by(cls, user_id=user_id))

    @classmethod
    async def create(cls, user_id: str) -> None:
        if not await cls.exists(user_id):
            await db.add(cls(user_id=user_id))

    @classmethod
    async def delete(cls, user_id: str) -> bool:
        if x := await db.get(cls, user_id=user_id):
            await db.delete(x)
            return True
        return False
