from pydantic import BaseModel, Extra, Field

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


class UserInfo(BaseModel):
    id: str = Field(description="Unique identifier for the user")
    name: str = Field(description="Unique username")
    display_name: str = Field(description="Full name of the user")
    email: str | None = Field(description="Email address")
    avatar_url: str | None = Field(description="URL of the user's avatar")

    class Config:
        extra = Extra.ignore

    def __str__(self) -> str:
        if self.name.lower() == self.display_name.lower():
            return self.display_name
        return f"{self.display_name} ({self.name})"
