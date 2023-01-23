from typing import cast

import icalendar

from api.schemas.calendar import Coaching, EventType, Webinar
from api.services.skills import get_skills
from api.utils.utc import utcfromtimestamp


async def create_ics(events: list[Webinar | Coaching]) -> bytes:
    cal = icalendar.Calendar()

    skill_names: dict[str, str] = {skill.id: skill.name for skill in await get_skills()}
    for e in events:
        event = icalendar.Event()
        skill = skill_names.get(e.skill_id) if e.skill_id else None
        if e.type == EventType.WEBINAR:
            summary = f"Webinar: {e.title} ({skill})" if skill else f"Webinar: {e.title}"
            description = e.description
        elif e.type == EventType.COACHING:
            summary = f"Coaching: {skill}" if skill else "Coaching"
            description = f"Instructor: {e.instructor} (Rating: {e.instructor_rating})"
            if not e.booked:
                summary = f"Empty Slot ({summary})"
            else:
                description += f"\nStudent: {cast(Coaching, e).student}"
        else:
            continue
        event.add("summary", summary)
        event.add("description", description)
        event.add("dtstart", utcfromtimestamp(e.start))
        event.add("dtend", utcfromtimestamp(e.start + e.duration * 60))
        if e.admin_link:
            event.add("location", e.admin_link)
        elif e.link:
            event.add("location", e.link)
        cal.add_component(event)

    return cast(bytes, cal.to_ical())
