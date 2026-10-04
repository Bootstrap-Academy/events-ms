from typing import Literal
from uuid import UUID

from pydantic import BaseModel, StrictStr, validator


class CancellationPreparation(BaseModel):
    kind: Literal["webinar", "coaching"]
    scope: Literal["auto", "booking", "session"] = "auto"

    class Config:
        extra = "forbid"


class CancellationDeclaration(BaseModel):
    target_id: UUID
    cancel_selected_scope: Literal[True]
    original_text: StrictStr
    administration_reason: StrictStr | None = None

    @validator("original_text", "administration_reason")
    @classmethod
    def meaningful_text(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 4000):
            raise ValueError("Provide the actual declaration or reason, up to 4000 characters")
        return value

    class Config:
        extra = "forbid"
