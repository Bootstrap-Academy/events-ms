from pydantic import BaseModel, Field

from api.schemas.user import UserInfo


class Exam(BaseModel):
    skill_id: str = Field(description="The ID of the skill")
    price: int = Field(description="The price of the exam")


class ExamSlot(BaseModel):
    id: str = Field(description="The ID of the exam slot")
    exam: Exam = Field(description="The exam")
    start: int = Field(description="The start time of the slot")
    end: int = Field(description="The end time of the slot")


class BookedExam(ExamSlot):
    student: UserInfo = Field(description="The ID of the student")
