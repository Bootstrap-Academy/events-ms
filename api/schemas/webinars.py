from pydantic import BaseModel, Field


class Webinar(BaseModel):
    id: str = Field(description="Webinar ID")
    skill_id: str = Field(description="ID of the skill")
    creator: str = Field(description="ID of the creator")
    creation_date: int = Field(description="Creation date")
    name: str = Field(description="Name of the webinar")
    description: str = Field(description="Description of the webinar")
    link: str | None = Field(description="Link to the webinar")
    start: int = Field(description="Start date")
    end: int = Field(description="End date")
    max_participants: int = Field(description="Maximum number of participants")
    price: int = Field(description="Price of the webinar")
    participants: int = Field(description="Number of registered participants")


class CreateWebinar(BaseModel):
    skill_id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the webinar")
    description: str = Field(description="Description of the webinar")
    link: str = Field(description="Link to the webinar")
    start: int = Field(description="Start date")
    duration: int = Field(gt=0, lt=24 * 60, description="Duration of the webinar in minutes")
    max_participants: int = Field(gt=0, description="Maximum number of participants")
    price: int = Field(ge=0, description="Price of the webinar")


class UpdateWebinar(BaseModel):
    name: str | None = Field(description="Name of the webinar")
    description: str | None = Field(description="Description of the webinar")
    link: str | None = Field(description="Link to the webinar")
    start: int | None = Field(description="Start date")
    duration: int | None = Field(gt=0, description="Duration of the webinar in minutes")
    max_participants: int | None = Field(gt=0, description="Maximum number of participants")
    price: int | None = Field(ge=0, description="Price of the webinar")
