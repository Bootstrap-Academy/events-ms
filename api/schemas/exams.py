from datetime import datetime

from pydantic import BaseModel, Field


class Exam(BaseModel):
    examiner: str = Field(description="The ID of the examiner")
    skill_id: str = Field(description="The ID of the skill")
    price: int = Field(description="The price of the exam")


class BookedExam(BaseModel):
    user_id: str = Field(description="The ID of the user")
    skill_id: str = Field(description="The ID of the skill")
    examiner_id: str = Field(description="The ID of the examiner")


class ExamSlot(BaseModel):
    price: int = Field(description="The price of the exam")
    start: datetime = Field(description="The start time of the slot")
    end: datetime = Field(description="The end time of the slot")
