from pydantic import BaseModel, Field


class CreateWebinar(BaseModel):
    skill_id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the webinar")
    description: str = Field(description="Description of the webinar")
    admin_link: str = Field(description="Meeting link for the instructor")
    link: str = Field(description="Meeting link for participants")
    start: int = Field(description="Start date")
    duration: int = Field(gt=0, lt=24 * 60, description="Duration of the webinar in minutes")
    max_participants: int = Field(ge=4, le=50, description="Maximum number of participants")
    price: int = Field(ge=0, description="Price of the webinar")


class UpdateWebinar(BaseModel):
    name: str | None = Field(description="Name of the webinar")
    description: str | None = Field(description="Description of the webinar")
    admin_link: str | None = Field(description="Meeting link for the instructor")
    link: str | None = Field(description="Link to the webinar")
    start: int | None = Field(description="Start date")
    duration: int | None = Field(gt=0, description="Duration of the webinar in minutes")
    max_participants: int | None = Field(gt=0, description="Maximum number of participants")
    price: int | None = Field(ge=0, description="Price of the webinar")
