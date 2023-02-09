from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc)


def utcfromtimestamp(ts: float) -> datetime:
    return datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc)


def datetime_link(dt: datetime) -> str:
    return f"https://www.timeanddate.com/worldclock/fixedtime.html?iso={dt.replace(tzinfo=None).isoformat()}"
