from datetime import datetime, time

from pydantic import BaseModel, Field


class Webinar(BaseModel):
    id: str = Field(description="ID of the webinar")
    skill_id: str = Field(description="ID of the skill the webinar belongs to")
    creation_date: datetime = Field(description="Point in time at which the webinar was created")
    name: str = Field(description="Title of the webinar")
    description: str = Field(description="Description of the webinar")
    link: str = Field(description="Meeting link of the webinar")
    admin_link: str = Field(description="Meeting link of the webinar for the instructor")
    start: datetime = Field(description="Start of the webinar")
    end: datetime = Field(description="End of the webinar")
    max_participants: int = Field(description="Maximum number of participants")
    price: int = Field(description="Price of the webinar in Morphcoins")
    participants: int = Field(description="Number of users who have booked the webinar")


class WebinarParticipation(BaseModel):
    webinar_id: str = Field(description="ID of the webinar")
    skill_id: str = Field(description="ID of the skill the webinar belongs to")
    name: str = Field(description="Title of the webinar")
    start: datetime = Field(description="Start of the webinar")


class Slot(BaseModel):
    id: str = Field(description="ID of the slot")
    start: datetime = Field(description="Start of the slot")
    end: datetime = Field(description="End of the slot")
    booked: bool = Field(description="Whether the slot has been booked")
    event_type: str | None = Field(description="Type of the booked event")
    skill_id: str | None = Field(description="ID of the skill the booked event belongs to")
    student_coins: int | None = Field(description="Price paid by the student in Morphcoins")
    instructor_coins: int | None = Field(description="Amount the instructor receives in Morphcoins")
    link: str | None = Field(description="Meeting link of the booked event")


class WeeklySlot(BaseModel):
    id: str = Field(description="ID of the weekly slot")
    weekday: int = Field(description="Weekday of the weekly slot (0 = Monday)")
    start: time = Field(description="Start time of the weekly slot")
    end: time = Field(description="End time of the weekly slot")


class Coaching(BaseModel):
    skill_id: str = Field(description="ID of the skill the user offers coachings for")
    price: int = Field(description="Price of the coaching in Morphcoins")


class Exam(BaseModel):
    skill_id: str = Field(description="ID of the skill the user offers exams for")


class LecturerRating(BaseModel):
    id: str = Field(description="ID of the rating")
    skill_id: str = Field(description="ID of the skill the webinar belonged to")
    webinar_timestamp: datetime = Field(description="Start of the webinar the rating refers to")
    webinar_name: str | None = Field(description="Title of the webinar, removed once the rating has been submitted")
    rating: int | None = Field(description="Rating that has been submitted, if any")


class UserDataExport(BaseModel):
    """Everything this service stores about a single user.

    User ids of other people (participants of a webinar, the instructor of a booked slot, the participant a rating
    was requested from) are left out, so the export never discloses anybody else's data. All points in time are
    ISO 8601 timestamps in UTC.
    """

    webinars: list[Webinar] = Field(description="Webinars the user has created")
    webinar_participations: list[WebinarParticipation] = Field(description="Webinars the user has booked")
    slots_offered: list[Slot] = Field(description="Slots the user offers as an instructor")
    slots_booked: list[Slot] = Field(description="Slots of other instructors the user has booked")
    weekly_slots: list[WeeklySlot] = Field(description="Recurring slots the user offers as an instructor")
    coachings: list[Coaching] = Field(description="Coachings the user offers")
    exams: list[Exam] = Field(description="Exams the user offers")
    emergency_cancel: bool = Field(description="Whether the user has cancelled an event on short notice")
    lecturer_ratings_received: list[LecturerRating] = Field(
        description="Ratings the user has received as an instructor"
    )
    lecturer_ratings_requested: list[LecturerRating] = Field(
        description="Ratings the user has been asked to submit as a participant"
    )
