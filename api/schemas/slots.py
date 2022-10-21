from pydantic import BaseModel, Field


class Slot(BaseModel):
    id: str = Field(description="Slot ID")
    start: int = Field(description="Start time of the slot")
    end: int = Field(description="End time of the slot")
    booked: bool = Field(description="Whether the slot is booked or not")


class CreateSlot(BaseModel):
    start: int = Field(description="Start time of the slot")
    duration: int = Field(gt=0, lt=24 * 60, description="Duration of the slot in minutes")


class WeeklySlot(BaseModel):
    id: str = Field(description="Weekly slot ID")
    weekday: int = Field(ge=0, le=6, description="Weekday of the weekly slot (0=Monday, 1=Tuesday, ...)")
    start: int = Field(description="Start time of the weekly slot (in minutes since midnight)")
    end: int = Field(description="End time of the weekly slot (in minutes since midnight)")


class CreateWeeklySlot(BaseModel):
    weekday: int = Field(ge=0, le=6, description="Weekday of the weekly slot (0=Monday, 1=Tuesday, ...)")
    start: int = Field(ge=0, lt=24 * 60, description="Start time of the weekly slot (in minutes since midnight)")
    end: int = Field(gt=0, lt=24 * 60, description="Start time of the weekly slot (in minutes since midnight)")
