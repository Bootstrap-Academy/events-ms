from typing import cast

from pydantic import BaseModel, Extra

from api.services.internal import InternalService
from api.utils.cache import redis_cached


class Skill(BaseModel):
    id: str
    name: str

    class Config:
        extra = Extra.ignore


@redis_cached("skills")
async def get_skills() -> list[Skill]:
    async with InternalService.SKILLS.client as client:
        response = await client.get("/skills")
        return [Skill.parse_obj(skill) for skill in response.json()]


async def get_skill(skill: str) -> Skill | None:
    return next(iter(s for s in await get_skills() if s.id == skill), None)


@redis_cached("skills", "skill")
async def get_skill_dependencies(skill: str) -> set[str] | None:
    async with InternalService.SKILLS.client as client:
        response = await client.get(f"/skills/{skill}/dependencies")
        if response.status_code != 200:
            return None
        return set(response.json())


@redis_cached("user_skills", "user_id")
async def get_skill_levels(user_id: str) -> dict[str, int]:
    async with InternalService.SKILLS.client as client:
        response = await client.get(f"/skills/{user_id}")
        return cast(dict[str, int], response.json())


@redis_cached("user_skills", "completed_skills")
async def get_lecturers(skill_id: str, level: int) -> set[str]:
    async with InternalService.SKILLS.client as client:
        response = await client.get(f"/graduates/{skill_id}", params={"level": level})
        return set(response.json())


async def add_xp(user_id: str, skill_id: str, xp: int) -> None:
    async with InternalService.SKILLS.client as client:
        await client.post(f"/skills/{user_id}/{skill_id}", json={"xp": xp})


async def apply_xp_benefit(operation: str, request: dict) -> dict:
    """Only the exact committed receiver contract establishes a delivery result."""
    import json
    from urllib.parse import quote

    from httpx import HTTPError

    from api.services.internal import InternalServiceError

    unknown = {"state": "uncertain", "reason": "Exact benefit receipt unavailable"}
    try:
        skill = quote(request["skill_id"], safe="")
        async with InternalService.SKILLS.client as client:
            client.event_hooks["response"] = []
            response = await client.post(
                f"/xp-operations/{operation}/{request['user_id']}/{skill}",
                json={"xp": request["xp"], "earning_id": request["earning_id"]},
                timeout=10,
            )
        if response.status_code == 409:
            return {"state": "review", "reason": "Exact benefit payload conflict"}
        if response.status_code != 200:
            # In particular404 does not prove the recipient was erased.
            return unknown
        result = response.json()
        if (
            not isinstance(result, dict)
            or result.get("operation_id") != operation
            or json.dumps(result.get("request"), sort_keys=True) != json.dumps(request, sort_keys=True)
            or not (
                (result.get("state") == "applied" and result.get("applied") is True)
                or (result.get("state") == "recipient_erased" and result.get("applied") is False)
            )
        ):
            return unknown
        return result
    except (HTTPError, InternalServiceError, ValueError, TypeError, KeyError):
        return unknown
