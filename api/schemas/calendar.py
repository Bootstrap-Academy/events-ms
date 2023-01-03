import enum

from pydantic import BaseModel, Field

from api.schemas.user import UserInfo


class EventType(enum.Enum):
    WEBINAR = "webinar"
    COACHING = "coaching"
    # EXAM = "exam"


class Event(BaseModel):
    id: str = Field(description="The ID of the event")
    type: EventType = Field(description="The type of the event")
    title: str | None = Field(description="The title of the event")
    description: str | None = Field(description="The description of the event")
    skill_id: str | None = Field(description="The skill ID")
    start: int = Field(description="The start time of the event")
    duration: int = Field(description="The duration of the event in minutes")
    price: int = Field(description="The price of the event in morphcoins per participant")
    admin_link: str | None = Field(description="The meeting link for the instructor")
    link: str | None = Field(description="The meeting link for participants")
    instructor: UserInfo | None = Field(description="The instructor")
    instructor_rating: float | None = Field(description="Rating of the instructor for the corresponding skill")
    booked: bool = Field(description="Whether the event is booked")
    bookable: bool = Field(description="Whether the event is bookable")


class Webinar(Event):
    type = Field(EventType.WEBINAR, const=True, description="The type of the event")
    creation_date: int = Field(description="Creation date")
    max_participants: int = Field(description="Maximum number of participants")
    participants: int = Field(description="Number of registered participants")


class Coaching(Event):
    type = Field(EventType.COACHING, const=True, description="The type of the event")
    student: UserInfo | None = Field(description="The student (booked coachings only)")


class Calendar(BaseModel):
    ics_token: str = Field(description="The token to access the calendar via the ics endpoint")
    events: list[Webinar | Coaching] = Field(description="List of events")
