from pydantic import BaseModel, Extra

from api.services.internal import InternalService
from api.settings import settings
from api.utils.cache import redis_cached


class Skill(BaseModel):
    id: str
    dependencies: set[str]

    class Config:
        extra = Extra.ignore


@redis_cached("skills")
async def get_skills() -> list[Skill]:
    async with InternalService.SKILLS.client as client:
        response = await client.get("/skills")
        return [Skill.parse_obj(skill) for skill in response.json()]


async def get_skill(skill: str) -> Skill | None:
    return next(iter(s for s in await get_skills() if s.id == skill), None)


@redis_cached("user_skills", "user_id")
async def get_completed_skills(user_id: str) -> set[str]:
    async with InternalService.SKILLS.client as client:
        response = await client.get(f"/skills/{user_id}")
        return set(response.json())


@redis_cached("user_skills", "completed_skills")
async def get_lecturers(completed_skills: set[str]) -> set[str]:
    async with InternalService.SKILLS.client as client:
        response = await client.get("/graduates", params={"skills": [*completed_skills]})
        return set(response.json())


async def complete_skill(user_id: str, skill_id: str) -> None:
    async with InternalService.SKILLS.client as client:
        await client.post(f"/skills/{user_id}/{skill_id}", json={"xp": settings.exam_xp, "complete": True})
