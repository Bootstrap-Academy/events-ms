from typing import cast

from pydantic import BaseModel, Extra

from api.services.internal import InternalService
from api.settings import settings
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
