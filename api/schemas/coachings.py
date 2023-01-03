from pydantic import BaseModel, Field

from api.schemas.user import UserInfo


class Coaching(BaseModel):
    skill_id: str = Field(description="The ID of the skill")
    price: int = Field(description="The price of the coaching")


class PublicCoaching(Coaching):
    instructor: UserInfo = Field(description="The instructor of the coaching")


class UpdateCoaching(BaseModel):
    price: int = Field(ge=0, description="The price of the coaching")


class CoachingSlot(BaseModel):
    id: str = Field(description="The ID of the coaching slot")
    coaching: PublicCoaching = Field(description="The coaching")
    start: int = Field(description="The start time of the slot")
    end: int = Field(description="The end time of the slot")
