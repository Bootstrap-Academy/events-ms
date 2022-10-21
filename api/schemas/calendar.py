import enum

from pydantic import BaseModel, Field

from api.schemas.coachings import Instructor


class EventType(enum.Enum):
    WEBINAR = "webinar"
    COACHING = "coaching"
    EXAM = "exam"


class Event(BaseModel):
    id: str = Field(description="The ID of the event")
    title: str | None = Field(description="The title of the event")
    description: str | None = Field(description="The description of the event")
    start: int = Field(description="The start time of the event")
    end: int = Field(description="The end time of the event")
    location: str | None = Field(description="The location of the event")
    type: EventType | None = Field(description="The type of the event")
    instructor: Instructor | None = Field(description="The instructor (webinars and coachings only)")
    student: Instructor | None = Field(description="The student (coachings and exams only)")
    skill_id: str | None = Field(description="The skill ID")
    booked: bool = Field(description="Whether the event is booked")


class Calendar(BaseModel):
    ics: str = Field(description="Private ics calendar url")
    ics_booked_only: str = Field(description="Private ics calendar url for booked events only")
    events: list[Event] = Field(description="List of events")
