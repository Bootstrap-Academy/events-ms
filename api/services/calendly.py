import json
import re
from datetime import date, datetime, timedelta

from httpx import AsyncClient
from pydantic import BaseModel


class EventType(BaseModel):
    uuid: str
    duration: int


async def get_event_type(url: str) -> EventType | None:
    async with AsyncClient() as client:
        response = await client.get(url)
        if response.status_code != 200:
            return None

        text = response.text

    for match in re.findall(r"\{.+}", text):
        try:
            data = json.loads(match)
        except json.JSONDecodeError:
            continue

        if not isinstance(data, dict):
            continue
        if not isinstance(modules := data.get("modules"), dict):
            continue
        if not isinstance(booking := modules.get("booking"), dict):
            continue
        if not isinstance(event_type := booking.get("event_type"), dict):
            continue

        try:
            return EventType.parse_obj(event_type)
        except ValueError:
            continue

    return None


async def get_available_slots(event_uuid: str, days: int = 30) -> list[datetime] | None:
    async with AsyncClient() as client:
        today = date.today()
        response = await client.get(
            f"https://calendly.com/api/booking/event_types/{event_uuid}/calendar/range",
            params={"timezone": "UTC", "range_start": str(today), "range_end": str(today + timedelta(days=days))},
        )
        if response.status_code != 200:
            return None

        return [
            datetime.fromisoformat(spot["start_time"].rstrip("Z"))
            for day in response.json().get("days", [])
            for spot in day.get("spots", [])
            if spot.get("status") == "available"
        ]
