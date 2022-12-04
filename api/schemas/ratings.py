from pydantic import BaseModel, Field

from api.schemas.coachings import Instructor


class Unrated(BaseModel):
    id: str = Field(description="Rating ID")
    instructor: Instructor = Field(description="The instructor who held the webinar")
    skill_id: str = Field(description="The webinar's skill ID")
    webinar_timestamp: int = Field(description="The webinar's start timestamp")
    webinar_name: str = Field(description="The webinar's name")
