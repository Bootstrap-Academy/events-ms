from pydantic import BaseModel, Field


class SetupEventType(BaseModel):
    api_token: str | None = Field(description="API token of the Calendly account")
    scheduling_url: str | None = Field(description="Public scheduling URL of the event type")
