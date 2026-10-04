"""Keep error diagnostics without application or request contents."""

from typing import Any


_EVENT_FIELDS = ("event_id", "timestamp", "platform", "level", "logger", "release", "environment", "sdk")
_FRAME_FIELDS = ("filename", "abs_path", "module", "function", "lineno", "colno", "in_app")


def _fields(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: value[name] for name in names if name in value}


def _stacktrace(value: dict[str, Any]) -> dict[str, Any]:
    # Frame locals and source excerpts may contain answers, code or credentials.
    return {"frames": [_fields(frame, _FRAME_FIELDS) for frame in value.get("frames", [])]}


def sanitize_sentry_breadcrumb(breadcrumb: Any, hint: Any) -> Any:
    """Retain technical breadcrumb metadata, never free text or payloads."""
    if breadcrumb.get("level") in ("debug", "trace"):
        return None
    safe = _fields(breadcrumb, ("timestamp", "level"))
    # Categories and types from custom breadcrumbs can themselves be free text.
    safe["category"] = "application"
    safe["type"] = "default"
    return safe


def sanitize_sentry_event(event: Any, hint: Any) -> Any:
    """Allow only diagnostic metadata; the same boundary covers all SDK producers."""
    # Attachments live in the hint and are added after before_send.
    hint.pop("attachments", None)
    # Performance spans have a separate, arbitrary payload schema. We report errors only.
    if event.get("type") == "transaction":
        return None

    safe = _fields(event, _EVENT_FIELDS)
    route = event.get("transaction", "")
    source = event.get("transaction_info", {}).get("source")
    # FastApiIntegration(transaction_style="url") supplies the matched route template.
    # Never fall back to the concrete request URL, even on an unmatched route.
    if source == "route" and isinstance(route, str) and route.startswith("/"):
        route = route.split("?", 1)[0].split("#", 1)[0]
        safe["transaction"] = route
        safe["transaction_info"] = {"source": "route"}
    else:
        route = ""

    if "request" in event:
        request = _fields(event["request"], ("method",))
        if route:
            request["url"] = route
        safe["request"] = request

    if "exception" in event:
        exceptions = []
        for exception in event["exception"].get("values", []):
            item = _fields(exception, ("type", "module"))
            if "mechanism" in exception:
                item["mechanism"] = _fields(exception["mechanism"], ("type", "handled", "synthetic"))
            if "stacktrace" in exception:
                item["stacktrace"] = _stacktrace(exception["stacktrace"])
            exceptions.append(item)
        safe["exception"] = {"values": exceptions}

    if "threads" in event:
        threads = []
        for thread in event["threads"].get("values", []):
            item = _fields(thread, ("id", "crashed", "current"))
            if "stacktrace" in thread:
                item["stacktrace"] = _stacktrace(thread["stacktrace"])
            threads.append(item)
        safe["threads"] = {"values": threads}

    if "logentry" in event:
        safe["logentry"] = {"message": "Application log"}

    breadcrumbs = event.get("breadcrumbs", {})
    if isinstance(breadcrumbs, dict):
        breadcrumbs = breadcrumbs.get("values", [])
    safe["breadcrumbs"] = {
        "values": [
            clean for breadcrumb in breadcrumbs if (clean := sanitize_sentry_breadcrumb(breadcrumb, {})) is not None
        ]
    }
    return safe
