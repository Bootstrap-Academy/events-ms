from datetime import datetime
from typing import cast

import icalendar

from api.schemas.calendar import Event


def create_ics(events: list[Event]) -> bytes:
    cal = icalendar.Calendar()

    for e in events:
        event = icalendar.Event()
        event.add("summary", e.title)
        if e.description:
            event.add("description", e.description)
        event.add("dtstart", datetime.fromtimestamp(e.start))
        event.add("dtend", datetime.fromtimestamp(e.end))
        if e.location:
            event.add("location", e.location)
        cal.add_component(event)

    return cast(bytes, cal.to_ical())
