from api.services.internal import InternalService
from api.settings import settings


async def get_skills() -> set[str]:
    async with InternalService.SKILLS.client as client:
        response = await client.get("/skills")
        return set(response.json())


async def exists_skill(skill: str) -> bool:
    return skill in await get_skills()


async def get_completed_skills(user_id: str) -> set[str]:
    async with InternalService.SKILLS.client as client:
        response = await client.get(f"/skills/{user_id}")
        return set(response.json())


async def get_lecturers(completed_skills: set[str]) -> set[str]:
    async with InternalService.SKILLS.client as client:
        response = await client.get("/graduates", params={"skills": [*completed_skills]})
        return set(response.json())


async def complete_skill(user_id: str, skill_id: str) -> None:
    async with InternalService.SKILLS.client as client:
        await client.post(f"/skills/{user_id}/{skill_id}", json={"xp": settings.exam_xp, "complete": True})
