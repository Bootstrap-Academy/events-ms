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
        return bool(await auth_redis.exists(f"access_token_invalidated:{self.rt}"))


class UserInfo(BaseModel):
    """Event response identity; events do not publish account names."""

    id: str = Field(description="Unique identifier for the user")
    avatar_url: str | None = Field(description="URL of the user's avatar")

    class Config:
        extra = Extra.ignore

    def __str__(self) -> str:
        return "User"


class UserDetails(BaseModel):
    """Internal details for existing contracts and support, never an event DTO."""

    id: str
    display_name: str
    avatar_url: str | None

    class Config:
        extra = Extra.ignore

    def __str__(self) -> str:
        return self.display_name
