import hmac
import re
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, cast

from httpx import AsyncClient
from pydantic import BaseModel, Extra

from api.database import db
from api.models.calendly_links import CalendlyLink
from api.redis import redis
from api.settings import settings


class User(BaseModel):
    uri: str
    current_organization: str
    name: str
    email: str
    timezone: str

    class Config:
        extra = Extra.ignore


class EventType(BaseModel):
    uri: str
    name: str
    scheduling_url: str
    active: bool
    duration: int

    class Config:
        extra = Extra.ignore


class Webhook(BaseModel):
    uri: str
    callback_url: str
    state: str

    class Config:
        extra = Extra.ignore


def get_client(api_token: str) -> AsyncClient:
    return AsyncClient(base_url="https://api.calendly.com", headers={"Authorization": f"Bearer {api_token}"})


async def fetch_paginated(client: AsyncClient, endpoint: str, params: dict[str, int | str]) -> AsyncIterator[Any]:
    page_token: str | None = None
    while True:
        if page_token:
            params["page_token"] = page_token

        response = await client.get(endpoint, params=params)
        if response.status_code != 200:
            return

        data = response.json()
        for item in data["collection"]:
            yield item

        if not (page_token := data["pagination"]["next_page_token"]):
            break


async def fetch_user(api_token: str) -> User | None:
    async with get_client(api_token) as client:
        response = await client.get("/users/me")
        if response.status_code != 200:
            return None

        try:
            return User.parse_obj(response.json()["resource"])
        except ValueError:
            return None


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
    out = None
    async with get_client(api_token) as client:
        async for event_type in fetch_paginated(client, "/event_types", {"user": user, "count": 100}):
            if scheduling_url:
                if event_type["scheduling_url"] == scheduling_url:
                    out = event_type
            elif out:
                raise ValueError
            else:
                out = event_type

    if not out:
        return None

    try:
        return EventType.parse_obj(out)
    except ValueError:
        return None


async def fetch_available_slots(api_token: str, event_type: str) -> list[datetime] | None:
    today = datetime.utcnow() + timedelta(hours=1)
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


async def fetch_webhooks(api_token: str, user: str, organization: str) -> list[Webhook]:
    async with get_client(api_token) as client:
        return [
            Webhook.parse_obj(webhook)
            async for webhook in fetch_paginated(
                client,
                "/webhook_subscriptions",
                {"user": user, "organization": organization, "scope": "user", "count": 100},
            )
        ]


async def create_webhook(api_token: str, user: str, organization: str, url: str, signing_key: str) -> Webhook | None:
    async with get_client(api_token) as client:
        response = await client.post(
            "/webhook_subscriptions",
            json={
                "url": url,
                "events": ["invitee.created", "invitee.canceled"],
                "organization": organization,
                "user": user,
                "scope": "user",
                "signing_key": signing_key,
            },
        )
        if response.status_code != 201:
            return None

        return Webhook.parse_obj(response.json()["resource"])


async def delete_webhook(api_token: str, uri: str) -> None:
    async with get_client(api_token) as client:
        await client.delete(f"/webhook_subscriptions/{uri.split('/')[-1]}")


async def update_webhooks(user: User, link: CalendlyLink, *, delete: bool = False) -> None:
    webhooks = [
        webhook
        for webhook in await fetch_webhooks(link.api_token, user.uri, user.current_organization)
        if webhook.callback_url.startswith(settings.public_base_url)
    ]

    callback_url = f"{settings.public_base_url.rstrip('/')}/calendly/{link.user_id}/webhook"

    found = False
    for webhook in webhooks:
        if webhook.callback_url != callback_url or delete:
            await delete_webhook(link.api_token, webhook.uri)
        else:
            found = True

    if not found and not delete:
        await create_webhook(
            link.api_token, user.uri, user.current_organization, callback_url, link.webhook_signing_key
        )


def verify_webhook_signature(payload: str, header: str, signing_key: str) -> bool:
    if not (match := re.match(r"^t=(\d+),v1=([a-z\d]+)$", header)):
        return False

    timestamp, signature = match.groups()
    payload = f"{timestamp}.{payload}"
    return hmac.digest(signing_key.encode(), payload.encode(), "sha256").hex() == signature


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
