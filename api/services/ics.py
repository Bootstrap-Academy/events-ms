from datetime import datetime
from typing import cast

from icalendar import Calendar, Event, vCalAddress, vText

from api.models import Webinar


def create_ics(webinar: Webinar) -> bytes:
    cal = Calendar()

    event = Event()
    event.add("summary", webinar.name)
    event.add("description", webinar.description)
    event.add("dtstart", webinar.start)
    event.add("dtend", webinar.end)
    event.add("location", webinar.link)
    cal.add_component(event)

    return cast(bytes, cal.to_ical())
