from pydantic import BaseModel, Extra, Field


class SetupEventType(BaseModel):
    api_token: str | None = Field(description="API token of the Calendly account")
    scheduling_url: str | None = Field(description="Public scheduling URL of the event type")


class WebhookPayload(BaseModel):
    event: str

    class Config:
        extra = Extra.ignore


class WebhookData(BaseModel):
    event: str
    payload: WebhookPayload

    class Config:
        extra = Extra.ignore
