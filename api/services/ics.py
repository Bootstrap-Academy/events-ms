from typing import cast

from icalendar import Calendar, Event

from api.models import Webinar
from api.settings import settings


def create_ics(webinars: list[tuple[Webinar, bool]], admin: bool) -> bytes:
    cal = Calendar()

    for webinar, booked in webinars:
        event = Event()
        event.add("summary", webinar.name)
        description = webinar.description
        if not booked and not admin:
            url = settings.webinar_registration_url.replace("WEBINAR_ID", webinar.id)
            description = f"You are not registered for this webinar. Register now: {url}\n\n{description}"
        event.add("description", description)
        event.add("dtstart", webinar.start)
        event.add("dtend", webinar.end)
        if booked or admin:
            event.add("location", webinar.link)
        cal.add_component(event)

    return cast(bytes, cal.to_ical())
