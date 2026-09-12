"""Actual internal wrapper over stub HTTP; local SQLite receipt/claim transactions.

The backend declarations are synthetic authority fixtures. No backend SQL or real
authentication, service, settlement, payment or mail is exercised here.
"""

import json
from copy import deepcopy
from datetime import timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, HTTPStatusError, MockTransport, ReadTimeout, Request, Response
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, db_context, filter_by, select
from api.models import BookingPayment, RetainedEventRight, SettlementClaim, Webinar, WebinarParticipant
from api.models.event_cancellation import EventCancellation, EventCancellationClaimEvidence
from api.models.settlement import CoinOperation
from api.services import event_cancellations, retained_events, shop
from api.services.internal import InternalService, InternalServiceError
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant
from tests.required import required
from tests.services.test_retained_events import booking, canonical
from tests.services.test_user_deletion import OTHER, THIRD, USER


def json_response(value: Any) -> Response:
    # httpx Response(json=None) means no body, not the JSON null receipt.
    return Response(200, content=json.dumps(value), headers={"Content-Type": "application/json"})


@pytest.fixture
def backend_http(mocker: MockerFixture) -> Iterator[SimpleNamespace]:
    requests: list[Request] = []
    clients: list[AsyncClient] = []
    stub = SimpleNamespace(reply=lambda request: json_response(None), requests=requests, clients=clients)
    # The local test environment has no service URL configured. Supply a
    # synthetic setting while exercising the real client's internal path join.
    mocker.patch.object(InternalService.SHOP, "_value_", "http://shop.synthetic.test/shop")

    async def handle(request: Any) -> Any:
        requests.append(request)
        return stub.reply(request)

    def client(*args: Any, **kwargs: Any) -> Any:
        # Keep the actual property's URL, headers and response hooks. Substitute
        # only its HTTP transport and a synthetic token, never shop.commercial.
        assert "transport" not in kwargs
        value = AsyncClient(*args, **kwargs, transport=MockTransport(handle))
        clients.append(value)
        return value

    stub.constructor = mocker.patch("api.services.internal.AsyncClient", side_effect=client)
    mocker.patch.object(InternalService, "_get_token", return_value="synthetic-shop-internal-proof")
    yield stub
    assert all(client.is_closed for client in clients)


INVALID_OBJECTS: list[Any] = [None, {}, True, 3, "unknown"]
INVALID_OUTCOMES: list[Any] = [None, [], False, 3, "unknown"]

OBJECT_OR_NULL = ["erasure", "register_event", "inventory", "event_cancellation_authority"]


@pytest.mark.parametrize(
    "operation,reply",
    [(operation, result) for operation in OBJECT_OR_NULL for result in [{"protocol": 1}, None]]
    + [
        ("event_cancellation_pending", []),
        ("event_cancellation_pending", [{"command_id": "original-locator"}]),
        ("event_cancellation_outcome", {"financial_satisfaction": False}),
    ],
)
async def test_actual_wrapper_preserves_operation_specific_containers_and_internal_request(
    backend_http: SimpleNamespace, operation: str, reply: Any
) -> None:
    body = {"source_subject": USER, "command_id": str(uuid4())}
    backend_http.reply = lambda request: json_response(reply)
    assert await shop.commercial(operation, body) == reply
    [request] = backend_http.requests
    assert request.method == "POST"
    assert str(request.url) == InternalService.SHOP.value.rstrip("/") + "/_internal/claims/" + operation
    assert request.headers["Authorization"] == "synthetic-shop-internal-proof"
    assert json.loads(request.content) == body
    assert backend_http.constructor.call_args.kwargs["event_hooks"] == {
        "response": [InternalService.SHOP._handle_error]
    }


@pytest.mark.parametrize(
    "operation,reply",
    [(operation, value) for operation in OBJECT_OR_NULL for value in [[], True, 3, "unknown"]]
    + [("event_cancellation_pending", value) for value in INVALID_OBJECTS]
    + [("event_cancellation_outcome", value) for value in INVALID_OUTCOMES],
)
async def test_wrong_container_never_becomes_an_empty_inventory_or_success(
    backend_http: SimpleNamespace, operation: str, reply: Any
) -> None:
    backend_http.reply = lambda request: json_response(reply)
    with pytest.raises(ValueError, match="Malformed"):
        await shop.commercial(operation, {})
    assert len(backend_http.requests) == 1


@pytest.mark.parametrize("operation", ["event_cancel", "event_rights", "unknown"])
async def test_public_or_unknown_operations_are_rejected_before_client_construction(
    backend_http: SimpleNamespace, operation: str
) -> None:
    with pytest.raises(ValueError, match="Unsupported commercial operation"):
        await shop.commercial(operation, {})
    backend_http.constructor.assert_not_called()
    assert backend_http.requests == []


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 500, 503])
async def test_real_internal_error_hooks_and_http_status_errors_propagate_without_fallback(
    backend_http: SimpleNamespace, status: int
) -> None:
    backend_http.reply = lambda request: Response(status, json={"detail": "synthetic unavailable"})
    expected = InternalServiceError if status in [401, 403, 500, 503] else HTTPStatusError
    with pytest.raises(expected):
        await shop.commercial("event_cancellation_authority", {"source_subject": USER})
    assert len(backend_http.requests) == 1
    assert backend_http.requests[0].url.path.endswith("/claims/event_cancellation_authority")


@pytest.mark.parametrize("failure", ["invalid_json", "empty_body", "read_timeout"])
async def test_invalid_json_or_lost_reply_propagates_without_another_request(
    backend_http: SimpleNamespace, failure: str
) -> None:
    def reply(request: Any) -> Any:
        if failure == "read_timeout":
            raise ReadTimeout("synthetic reply loss", request=request)
        return Response(200, content=b"" if failure == "empty_body" else b"{invalid-json")

    backend_http.reply = reply
    with pytest.raises(ReadTimeout if failure == "read_timeout" else json.JSONDecodeError):
        await shop.commercial("event_cancellation_outcome", {"command_id": str(uuid4())})
    assert len(backend_http.requests) == 1


async def preserved_booking() -> SimpleNamespace:
    event, seat = await booking()
    payment = required(await db.get(BookingPayment, id=seat.payment_id))
    payment.original = {"commercial_event": {"instructor_id": OTHER}}
    await retained_events.lock_subject(USER)
    await db.first(filter_by(Webinar, id=event.id).with_for_update())
    await retained_events.preserve_on_erasure(
        USER, SimpleNamespace(canonical=canonical(USER), observed_at=utcnow()), payment, event, "participant"
    )
    right = required(await retained_events.right_for(payment.id, "participant"))
    await db.commit()
    command = str(uuid4())
    instant = utcnow().replace(microsecond=654321).astimezone(timezone(timedelta(hours=2)))
    receipt = {
        "protocol": 1,
        "command_id": command,
        "source_subject": USER,
        "right_id": right.id,
        "received_at": instant.isoformat(),
        "purpose": "cancel_identified_event_contract",
        "source": "authenticated_claimant_declaration",
        "declaration": {
            "command_id": command,
            "source_subject": USER,
            "right_id": right.id,
            "cancel_identified_contract": True,
            "original_text": "Cancel this exact original booking; retain this original statement.",
        },
    }
    return SimpleNamespace(
        event_id=event.id,
        payment_id=payment.id,
        right_id=right.id,
        receipt=receipt,
        command=command,
        received=instant,
        payment_original=deepcopy(payment.original),
        payment_evidence=deepcopy(payment.evidence),
    )


async def test_actual_intake_wrapper_preserves_original_receipt_and_replays_before_new_booking_selection(
    session: AsyncSession, backend_http: SimpleNamespace, mocker: MockerFixture
) -> None:
    original = required(await preserved_booking())
    mocker.patch("api.services.settlements.finish", new_callable=AsyncMock)
    backend_http.reply = lambda request: json_response(original.receipt)
    first = await event_cancellations.receive(USER, original.command)
    assert first["state"] == "applied" and first["financial_satisfaction"] is False
    assert first["received_at"] == original.received.astimezone(timezone.utc).isoformat()
    [request] = backend_http.requests
    assert json.loads(request.content) == {"source_subject": USER, "command_id": original.command}
    replacement = paid_participant(webinar_id=original.event_id, user_id=USER, paid_coins=42)
    await db.add(replacement)
    await db.commit()
    assert await event_cancellations.receive(USER, original.command) == first
    assert len(backend_http.requests) == 1
    assert (required(await db.get(WebinarParticipant, payment_id=replacement.payment_id))).user_id == USER
    assert (required(await db.get(EventCancellation, id=original.command))).original == original.receipt
    [claim] = await db.all(select(SettlementClaim))
    assert claim.user_id == USER and claim.payment_ids == [original.payment_id] and claim.coins == 1337
    assert len(await db.all(select(EventCancellationClaimEvidence))) == 1
    [operation] = await db.all(select(CoinOperation))
    assert operation.id == claim.id and operation.completed_at is None
    payment = required(await db.get(BookingPayment, id=original.payment_id))
    assert payment.user_id == USER and payment.original == original.payment_original
    assert payment.evidence == original.payment_evidence and payment.paid_coins == 1337


@pytest.mark.parametrize("invalid", ["null", "owner", "right", "origin", "command"])
async def test_unavailable_or_mismatched_authority_creates_no_local_declaration(
    session: AsyncSession, backend_http: SimpleNamespace, invalid: Any
) -> None:
    original = required(await preserved_booking())
    authority = deepcopy(original.receipt)
    if invalid == "null":
        authority = None
    elif invalid == "owner":
        authority["source_subject"] = THIRD
    elif invalid == "right":
        authority["declaration"]["right_id"] = str(uuid4())
    elif invalid == "origin":
        authority["source"] = "ordinary_authenticated"
    else:
        authority["command_id"] = str(uuid4())
    backend_http.reply = lambda request: json_response(authority)
    with pytest.raises(HTTPException) as error:
        await event_cancellations.receive(USER, original.command)
    assert error.value.status_code == 503
    assert await db.all(select(EventCancellation)) == []
    assert await db.all(select(SettlementClaim)) == []
    assert (required(await db.get(RetainedEventRight, id=original.right_id))).state == "preserved"
    assert (required(await db.get(WebinarParticipant, payment_id=original.payment_id))).user_id == USER
    assert len(backend_http.requests) == 1


async def test_first_receipt_survives_application_failure_after_actual_authority_http(
    session: AsyncSession, backend_http: SimpleNamespace, mocker: MockerFixture
) -> None:
    original = required(await preserved_booking())
    backend_http.reply = lambda request: json_response(original.receipt)
    mocker.patch.object(event_cancellations, "process", side_effect=RuntimeError("synthetic apply failure"))
    with pytest.raises(RuntimeError, match="synthetic apply failure"):
        await event_cancellations.receive(USER, original.command)
    await db.session.rollback()
    saved = required(await db.get(EventCancellation, id=original.command))
    assert saved.original == original.receipt and saved.result is None
    assert await db.all(select(SettlementClaim)) == []
    assert len(backend_http.requests) == 1


@pytest.mark.parametrize("inventory", [[], None])
async def test_empty_recovery_is_valid_but_null_inventory_is_failure(
    backend_http: SimpleNamespace, inventory: Any
) -> None:
    backend_http.reply = lambda request: json_response(inventory)
    if inventory is None:
        with pytest.raises(ValueError, match="Malformed cancellation inventory"):
            await event_cancellations.recover()
    else:
        await event_cancellations.recover()
    assert len(backend_http.requests) == 1
    assert backend_http.requests[0].url.path.endswith("/claims/event_cancellation_pending")


async def test_recovery_skips_bad_locator_and_replays_exact_commit_after_lost_outcome_response(
    session: AsyncSession, backend_http: SimpleNamespace, mocker: MockerFixture
) -> None:
    original = required(await preserved_booking())
    mocker.patch("api.services.settlements.finish", new_callable=AsyncMock)
    pending = [
        {"source_subject": "malformed"},
        {"source_subject": USER, "command_id": original.command, "right_id": original.right_id},
    ]
    observations: list[Any] = []
    lose_first_final = True

    def reply(request: Request) -> Any:
        nonlocal lose_first_final
        operation = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if operation == "event_cancellation_pending":
            assert body == {}
            return json_response(pending)
        if operation == "event_cancellation_authority":
            assert observations[-1]["state"] == "uncertain"
            assert body == {"source_subject": USER, "command_id": original.command}
            return json_response(original.receipt)
        assert operation == "event_cancellation_outcome"
        assert body["command_id"] == original.command
        outcome = body["outcome"]
        assert (outcome["source_subject"], outcome["right_id"]) == (USER, original.right_id)
        assert outcome["financial_satisfaction"] is False
        observations.append(deepcopy(outcome))
        if outcome["state"] == "applied" and lose_first_final:
            lose_first_final = False
            # Simulate remote observation accepted but its response lost. This
            # stub is not evidence of backend SQL deduplication or HTTP pairing.
            raise ReadTimeout("synthetic final outcome response lost", request=request)
        return Response(
            200, json={"declaration": original.receipt, "service_outcome": outcome, "financial_satisfaction": False}
        )

    backend_http.reply = reply
    await event_cancellations.recover()
    async with db_context():
        saved = required(await db.get(EventCancellation, id=original.command))
        first = deepcopy(required(saved.result))
        assert saved.original == original.receipt and first["state"] == "applied"
        [claim] = await db.all(select(SettlementClaim))
        claim_id = claim.id
        assert claim.payment_ids == [original.payment_id] and claim.user_id == USER
        [operation] = await db.all(select(CoinOperation))
        assert operation.id == claim_id and operation.completed_at is None
        replacement = paid_participant(webinar_id=original.event_id, user_id=USER, paid_coins=42)
        await db.add(replacement)
        replacement_id = replacement.payment_id
    await event_cancellations.recover()
    assert [value["state"] for value in observations] == ["uncertain", "applied", "uncertain", "applied"]
    assert observations[1] == observations[3] == first
    assert observations[0]["attempt_id"] != observations[2]["attempt_id"]
    operations = [request.url.path.rsplit("/", 1)[-1] for request in backend_http.requests]
    assert operations == [
        "event_cancellation_pending",
        "event_cancellation_outcome",
        "event_cancellation_authority",
        "event_cancellation_outcome",
        "event_cancellation_pending",
        "event_cancellation_outcome",
        "event_cancellation_outcome",
    ]
    async with db_context():
        [saved] = await db.all(select(EventCancellation))
        assert saved.original == original.receipt and saved.result == first
        [claim] = await db.all(select(SettlementClaim))
        assert claim.id == claim_id and claim.payment_ids == [original.payment_id]
        [operation] = await db.all(select(CoinOperation))
        assert operation.id == claim_id and operation.completed_at is None
        assert len(await db.all(select(EventCancellationClaimEvidence))) == 1
        assert (required(await db.get(WebinarParticipant, payment_id=replacement_id))).user_id == USER
        payment = required(await db.get(BookingPayment, id=original.payment_id))
        assert payment.user_id == USER and payment.original == original.payment_original
        assert payment.evidence == original.payment_evidence and payment.paid_coins == 1337
