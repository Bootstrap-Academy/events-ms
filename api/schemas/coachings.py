from pydantic import BaseModel, Extra, Field


class Instructor(BaseModel):
    id: str = Field(description="Unique identifier for the user")
    name: str = Field(description="Unique username")
    display_name: str = Field(description="Full name of the user")
    email: str = Field(description="Email address")
    avatar_url: str = Field(description="URL of the user's avatar")

    class Config:
        extra = Extra.ignore

    def __str__(self) -> str:
        if self.name.lower() == self.display_name.lower():
            return self.display_name
        return f"{self.display_name} ({self.name})"


class Coaching(BaseModel):
    skill_id: str = Field(description="The ID of the skill")
    price: int = Field(description="The price of the coaching")


class PublicCoaching(Coaching):
    instructor: Instructor = Field(description="The instructor of the coaching")


class UpdateCoaching(BaseModel):
    price: int = Field(ge=0, description="The price of the coaching")


class CoachingSlot(BaseModel):
    id: str = Field(description="The ID of the coaching slot")
    coaching: PublicCoaching = Field(description="The coaching")
    start: float = Field(description="The start time of the slot")
    end: float = Field(description="The end time of the slot")
