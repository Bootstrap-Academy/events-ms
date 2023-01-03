from pydantic import BaseModel, Field

from api.schemas.user import UserInfo


class Coaching(BaseModel):
    skill_id: str = Field(description="The ID of the skill")
    price: int = Field(description="The price of the coaching")


class PublicCoaching(Coaching):
    instructor: UserInfo = Field(description="The instructor of the coaching")


class UpdateCoaching(BaseModel):
    price: int = Field(ge=0, description="The price of the coaching")
