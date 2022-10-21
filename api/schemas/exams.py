from pydantic import BaseModel, Field

from api.schemas.coachings import Instructor


class Exam(BaseModel):
    skill_id: str = Field(description="The ID of the skill")
    price: int = Field(description="The price of the exam")


class ExamSlot(BaseModel):
    id: str = Field(description="The ID of the exam slot")
    exam: Exam = Field(description="The exam")
    start: float = Field(description="The start time of the slot")
    end: float = Field(description="The end time of the slot")


class BookedExam(ExamSlot):
    student: Instructor = Field(description="The ID of the student")
