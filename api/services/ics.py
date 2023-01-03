from typing import cast

import icalendar

from api.schemas.calendar import Event
from api.utils.utc import utcfromtimestamp


def create_ics(events: list[Event]) -> bytes:
    cal = icalendar.Calendar()

    for e in events:
        event = icalendar.Event()
        event.add("summary", e.title)
        if e.description:
            event.add("description", e.description)
        event.add("dtstart", utcfromtimestamp(e.start))
        event.add("dtend", utcfromtimestamp(e.start + e.duration * 60))
        if e.admin_link:
            event.add("location", e.admin_link)
        elif e.link:
            event.add("location", e.link)
        cal.add_component(event)

    return cast(bytes, cal.to_ical())
