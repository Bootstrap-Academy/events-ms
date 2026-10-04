"""Close the retired public offer without interrupting existing account rights.

Attach this dependency before authentication and other dependencies on each
retired route. A stale client cannot create an offer, reserve funds, issue a
calendar token or trigger a rating message. Exact cancellations and internal
export, deletion and settlement handlers deliberately do not use this guard.
"""

from typing import NoReturn

from fastapi import Depends, HTTPException


def reject_event_offering() -> NoReturn:
    raise HTTPException(
        status_code=410,
        detail={"code": "EventOfferingClosed", "message": "Webinare und Coachings bieten wir nicht mehr an."},
    )


def reject_calendar() -> NoReturn:
    raise HTTPException(
        status_code=410, detail={"code": "EventCalendarClosed", "message": "Der Kalender ist nicht mehr verfügbar."}
    )


closed_offering = Depends(reject_event_offering)
closed_calendar = Depends(reject_calendar)
