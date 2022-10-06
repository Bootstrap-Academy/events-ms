from datetime import datetime

from pydantic import BaseModel, Field


class Coaching(BaseModel):
    instructor: str = Field(description="The ID of the instructor")
    skill_id: str = Field(description="The ID of the skill")
    price: int = Field(description="The price of the coaching")


class UpdateCoaching(BaseModel):
    price: int = Field(ge=0, description="The price of the coaching")


class CoachingSlot(BaseModel):
    coaching: Coaching = Field(description="The coaching")
    start: datetime = Field(description="The start time of the slot")
    end: datetime = Field(description="The end time of the slot")
