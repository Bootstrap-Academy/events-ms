from pydantic import BaseModel, Extra

from api.redis import auth_redis


class User(BaseModel):
    id: str
    email_verified: bool
    admin: bool


class UserAccessTokenData(BaseModel):
    email_verified: bool
    admin: bool

    class Config:
        extra = Extra.ignore


class UserAccessToken(BaseModel):
    uid: str
    rt: str
    data: UserAccessTokenData

    class Config:
        extra = Extra.ignore

    def to_user(self) -> User:
        return User(id=self.uid, **self.data.dict())

    async def is_revoked(self) -> bool:
        return bool(await auth_redis.exists(f"session_logout:{self.rt}"))
