from datetime import date, datetime, timedelta
from typing import cast

from httpx import AsyncClient
from pydantic import BaseModel, Extra


class EventType(BaseModel):
    uri: str
    name: str
    active: bool
    duration: int

    class Config:
        extra = Extra.ignore


def get_client(api_token: str) -> AsyncClient:
    return AsyncClient(base_url="https://api.calendly.com", headers={"Authorization": f"Bearer {api_token}"})


async def get_user(api_token: str) -> str | None:
    async with get_client(api_token) as client:
        response = await client.get("/users/me")
        if response.status_code != 200:
            return None

        return cast(str, response.json()["resource"]["uri"])


async def get_event_type(api_token: str, event_type: str) -> EventType | None:
    async with get_client(api_token) as client:
        response = await client.get(f"/event_types/{event_type.split('/')[-1]}")
        if response.status_code != 200:
            return None

        try:
            return EventType.parse_obj(response.json()["resource"])
        except ValueError:
            return None


async def find_event_type(api_token: str, user: str, scheduling_url: str | None) -> EventType | None:
    page_token: str | None = None
    async with get_client(api_token) as client:
        params: dict[str, int | str] = {"user": user, "count": 100}
        if page_token:
            params["page_token"] = page_token

        response = await client.get("/event_types", params=params)
        if response.status_code != 200:
            return None

        collection = response.json()["collection"]

    if not scheduling_url and len(collection) > 1:
        raise ValueError

    for event_type in collection:
        if not scheduling_url or event_type["scheduling_url"] == scheduling_url:
            try:
                return EventType.parse_obj(event_type)
            except ValueError:
                return None

    return None


async def get_available_slots(api_token: str, event_type: str) -> list[datetime] | None:
    today = date.today()
    async with get_client(api_token) as client:
        response = await client.get(
            "/event_type_available_times",
            params={"event_type": event_type, "start_time": str(today), "end_time": str(today + timedelta(days=7))},
        )
        if response.status_code != 200:
            return None

        return [
            datetime.fromisoformat(slot["start_time"].rstrip("Z"))
            for slot in response.json()["collection"]
            if slot["status"] == "available"
        ]


async def create_single_use_link(api_token: str, event_type: str) -> str | None:
    async with get_client(api_token) as client:
        response = await client.post(
            "/scheduling_links", json={"owner": event_type, "owner_type": "EventType", "max_event_count": 1}
        )
        if response.status_code != 201:
            return None

        return cast(str, response.json()["resource"]["booking_url"])
