from datetime import date, datetime, timedelta
from typing import cast

from httpx import AsyncClient
from pydantic import BaseModel, Extra

from api.database import db
from api.models.calendly_links import CalendlyLink
from api.redis import redis
from api.settings import settings


class EventType(BaseModel):
    uri: str
    name: str
    scheduling_url: str
    active: bool
    duration: int

    class Config:
        extra = Extra.ignore


def get_client(api_token: str) -> AsyncClient:
    return AsyncClient(base_url="https://api.calendly.com", headers={"Authorization": f"Bearer {api_token}"})


async def fetch_user(api_token: str) -> str | None:
    async with get_client(api_token) as client:
        response = await client.get("/users/me")
        if response.status_code != 200:
            return None

        return cast(str, response.json()["resource"]["uri"])


async def fetch_event_type(api_token: str, event_type: str) -> EventType | None:
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


async def fetch_available_slots(api_token: str, event_type: str) -> list[datetime] | None:
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


async def get_calendly_link(user_id: str) -> EventType | None:
    if cached := await redis.get(key := f"calendly_link:{user_id}"):
        return EventType.parse_raw(cached)

    link = await db.get(CalendlyLink, user_id=user_id)
    if not link:
        await redis.delete(key)
        return None

    return await update_calendly_link_cache(user_id, link)


async def update_calendly_link_cache(user_id: str, link: CalendlyLink | None = None) -> EventType | None:
    if not link:
        link = await db.get(CalendlyLink, user_id=user_id)
        if not link:
            await clear_calendly_link_cache(user_id)
            return None

    out = await fetch_event_type(link.api_token, link.uri)
    if out:
        await redis.setex(f"calendly_link:{user_id}", settings.calendly_cache_ttl, out.json())
    else:
        await clear_calendly_link_cache(user_id)
    return out


async def clear_calendly_link_cache(user_id: str) -> None:
    await redis.delete(f"calendly_link:{user_id}")
