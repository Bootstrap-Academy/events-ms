import json
from typing import Any

import pytest

from api.telemetry import sanitize_sentry_breadcrumb, sanitize_sentry_event


def test_event_removes_private_contents_and_keeps_error_location() -> None:
    private = "PRIVATE-ANSWER-CODE-CREDENTIAL"
    frame = {
        "filename": "api/endpoints/rooms.py",
        "function": "save_state",
        "lineno": 42,
        "vars": {"answer": private},
        "context_line": private,
        "pre_context": [private],
        "post_context": [private],
    }
    event = {
        "event_id": "diagnostic-id",
        "level": "error",
        "release": "service@1",
        "transaction": "/rooms/{room_id}/state",
        "transaction_info": {"source": "route"},
        "request": {
            "url": f"https://user:{private}@example.invalid/rooms/{private}/state?email={private}#{private}",
            "method": "POST",
            "data": {"answer": private, "code": private},
            "query_string": private,
            "headers": {"Authorization": private, "X-Api-Key": private},
            "cookies": {"session": private},
            "env": {"REMOTE_ADDR": private},
            "path_params": {"room_id": private},
        },
        "message": private,
        "logentry": {"message": private, "formatted": private, "params": [private]},
        "user": {"email": private},
        "extra": {"code": private},
        "contexts": {"request": {"data": private}},
        "tags": {"answer": private},
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": private,
                    "stacktrace": {"frames": [frame]},
                    "mechanism": {"type": "asgi", "handled": False, "data": private},
                }
            ]
        },
        "threads": {"values": [{"id": 1, "name": private, "stacktrace": {"frames": [frame]}}]},
        "breadcrumbs": {
            "values": [
                {"level": "debug", "message": private},
                {"level": "info", "message": private, "category": private, "data": {"url": private}},
            ]
        },
        "unknown_future_field": private,
    }
    hint = {"attachments": [private]}
    clean = sanitize_sentry_event(event, hint)

    assert private not in json.dumps(clean)
    assert "attachments" not in hint
    assert clean["event_id"] == "diagnostic-id"
    assert clean["release"] == "service@1"
    assert clean["request"] == {"method": "POST", "url": "/rooms/{room_id}/state"}
    assert clean["transaction"] == "/rooms/{room_id}/state"
    exception = clean["exception"]["values"][0]
    assert exception["type"] == "ValueError"
    assert exception["stacktrace"]["frames"] == [
        {"filename": "api/endpoints/rooms.py", "function": "save_state", "lineno": 42}
    ]
    assert exception["mechanism"] == {"type": "asgi", "handled": False}
    assert clean["threads"]["values"][0]["stacktrace"] == exception["stacktrace"]
    assert clean["logentry"] == {"message": "Application log"}
    assert clean["breadcrumbs"]["values"] == [{"level": "info", "category": "application", "type": "default"}]


@pytest.mark.parametrize("source", ["url", "component", None])
def test_unmatched_or_concrete_transaction_is_not_a_route_template(source: str | None) -> None:
    event = {
        "transaction": "/rooms/PRIVATE-IDENTIFIER",
        "transaction_info": {"source": source},
        "request": {"method": "GET", "url": "https://example.invalid/PRIVATE-IDENTIFIER?email=PRIVATE"},
    }
    clean = sanitize_sentry_event(event, {})
    assert clean["request"] == {"method": "GET"}
    assert "transaction" not in clean


@pytest.mark.parametrize("level", ["debug", "trace"])
def test_low_level_breadcrumb_is_dropped(level: str) -> None:
    assert sanitize_sentry_breadcrumb({"level": level, "message": "private"}, {}) is None


@pytest.mark.parametrize("level", ["info", "warning", "error"])
def test_breadcrumb_keeps_level_without_arbitrary_payload(level: str) -> None:
    breadcrumb = {"level": level, "message": "private", "category": "private", "data": {"body": "private"}}
    assert sanitize_sentry_breadcrumb(breadcrumb, {}) == {"level": level, "category": "application", "type": "default"}


def test_transactions_do_not_send_spans_or_attachments() -> None:
    hint: dict[str, Any] = {"attachments": ["private"]}
    assert sanitize_sentry_event({"type": "transaction", "spans": [{"description": "private"}]}, hint) is None
    assert hint == {}
