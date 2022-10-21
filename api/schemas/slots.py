from pydantic import BaseModel, Field


class Slot(BaseModel):
    id: str = Field(description="Slot ID")
    start: float = Field(description="Start time of the slot")
    end: float = Field(description="End time of the slot")
    booked: bool = Field(description="Whether the slot is booked or not")


class CreateSlot(BaseModel):
    start: float = Field(description="Start time of the slot")
    duration: float = Field(gt=0, description="Duration of the slot in minutes")
