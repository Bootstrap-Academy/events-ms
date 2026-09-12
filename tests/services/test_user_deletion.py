from copy import deepcopy
from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Any, Iterator
from unittest.mock import AsyncMock, call
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, select
from api.models import (
    BookingPayment,
    CalendarToken,
    Coaching,
    EmergencyCancel,
    EventType,
    Exam,
    LecturerRating,
    Slot,
    Webinar,
    WebinarParticipant,
    WeeklySlot,
)
from api.models.booking_payment import CommercialErasureReceipt, CommercialHandoff, SettlementClaim
from api.models.retained_event import EventSubjectGuard, RetainedEventErasure, RetainedEventRight
from api.models.settlement import CoinOperation, SettlementBatch
from api.services.user_deletion import USER_CACHE_PREFIXES, delete_user_data
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant, paid_slot


USER = "40ab0e5c-b7ee-4a25-9d10-1eaf3c62d2bd"
OTHER = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"
THIRD = "c1d2eb59-8b1a-4a2f-9c37-1b8e5d7f60a3"
UNKNOWN = "cb3b0d6e-8e1b-4b7c-9d64-c7a1a5a1e6a5"


def canonical_erasure(subject: str, declaration: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "protocol": 1,
        "subject": subject,
        "case_id": str(uuid4()),
        "request": {
            "id": str(uuid4()),
            "source": "authenticated_service_receipt",
            "received_at": utcnow().isoformat(),
            "evidence": {"declaration": declaration or {"paid_contract_intent": "none"}},
        },
    }


@dataclass
class CommercialResponses:
    """Explicit synthetic remote receipts; local erasure and settlement code stays real."""

    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    registered: dict[str, dict[str, Any]] = field(default_factory=dict)
    inventory_failure: str | None = None
    handoff_failure: str | None = None
    unexpected: list[str] = field(default_factory=list)

    async def __call__(self, operation: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.requests.append((operation, deepcopy(payload)))
        if operation == "erasure":
            assert set(payload) == {"subject"}
            if payload["subject"] in self.unavailable:
                raise RuntimeError("Synthetic canonical service unavailable")
            return deepcopy(self.receipts.get(payload["subject"]))
        if operation == "inventory":
            assert payload["service"] == "events" and payload["state"] == "erased"
            if self.inventory_failure == "lost":
                raise RuntimeError("Synthetic inventory reply lost")
            return {"accepted": True, "financial_satisfaction": self.inventory_failure == "financial_satisfaction"}
        if operation == "register_event":
            assert payload["identity"]["source_key"] == payload["obligation_id"]
            command_id = payload["command_id"]
            if command_id in self.registered:
                assert self.registered[command_id] == payload
            self.registered[command_id] = deepcopy(payload)
            if self.handoff_failure == "lost":
                raise RuntimeError("Synthetic accepted handoff reply lost")
            return {
                "protocol": 1,
                "obligation_id": "wrong-obligation" if self.handoff_failure == "identity" else payload["obligation_id"],
                "disposition": "unknown" if self.handoff_failure == "disposition" else "claim_preserved",
            }
        self.unexpected.append(operation)
        raise AssertionError(f"Unexpected commercial operation: {operation}")

    def bodies(self, operation: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.requests if name == operation]


def _webinar(webinar_id: str, creator: str) -> Webinar:
    return Webinar(
        id=webinar_id,
        skill_id="test",
        creator=creator,
        creation_date=utcnow(),
        name="test webinar",
        description="test description",
        admin_link="https://meet.jit.si/admin",
        link="https://meet.jit.si/link",
        start=utcnow() + timedelta(days=1),
        end=utcnow() + timedelta(days=1, hours=1),
        max_participants=42,
        price=1337,
    )


def _slot(slot_id: str, user_id: str, booked_by: str | None, weekly_slot_id: str | None = None) -> Slot:
    return paid_slot(
        id=slot_id,
        user_id=user_id,
        start=utcnow() + timedelta(days=1),
        end=utcnow() + timedelta(days=1, hours=1),
        booked_by=booked_by,
        event_type=EventType.COACHING if booked_by else None,
        student_coins=42 if booked_by else None,
        instructor_coins=21 if booked_by else None,
        skill_id="test" if booked_by else None,
        admin_link="https://meet.jit.si/admin" if booked_by else None,
        link="https://meet.jit.si/link" if booked_by else None,
        weekly_slot_id=weekly_slot_id,
    )


def _weekly_slot(weekly_slot_id: str, user_id: str) -> WeeklySlot:
    return WeeklySlot(
        id=weekly_slot_id, user_id=user_id, weekday=3, start=time(10, 0), end=time(11, 0), last_slot=utcnow()
    )


def _rating(rating_id: str, lecturer_id: str, participant_id: str | None) -> LecturerRating:
    return LecturerRating(
        id=rating_id,
        lecturer_id=lecturer_id,
        participant_id=participant_id,
        skill_id="test",
        webinar_timestamp=utcnow(),
        webinar_name="test webinar",
        rating=None if participant_id else 5,
    )


@pytest.fixture(autouse=True)
def clear_cache_patch(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())


@pytest.fixture(autouse=True)
def commercial_remote(mocker: MockerFixture) -> Iterator[CommercialResponses]:
    remote = CommercialResponses(
        receipts={subject: canonical_erasure(subject) for subject in (USER, OTHER, THIRD, UNKNOWN)}
    )
    mocker.patch("api.services.shop.commercial", side_effect=remote.__call__)
    legacy = mocker.patch("api.services.shop.apply_coin_operation", AsyncMock())
    yield remote
    assert remote.unexpected == []
    legacy.assert_not_awaited()


@pytest.fixture
async def data(session: AsyncSession) -> None:
    # webinars, including the ones the users have booked
    await db.add(_webinar("webinar-user", USER))
    await db.add(_webinar("webinar-other", OTHER))
    await db.add(_webinar("webinar-third", THIRD))
    await db.add(paid_participant(webinar_id="webinar-user", user_id=OTHER, paid_coins=1337))
    await db.add(paid_participant(webinar_id="webinar-other", user_id=USER, paid_coins=1337))
    await db.add(paid_participant(webinar_id="webinar-third", user_id=USER, paid_coins=1337))
    await db.add(paid_participant(webinar_id="webinar-third", user_id=OTHER, paid_coins=1337))

    # slots the users offer as lecturers, one of them booked by the respective other user
    await db.add(_weekly_slot("weekly-user", USER))
    await db.add(_weekly_slot("weekly-other", OTHER))
    await db.add(_slot("slot-user", USER, None, "weekly-user"))
    await db.add(_slot("slot-user-booked", USER, OTHER))
    await db.add(_slot("slot-other", OTHER, None, "weekly-other"))
    await db.add(_slot("slot-other-booked", OTHER, USER))

    await db.add(Coaching(user_id=USER, skill_id="test", price=42))
    await db.add(Exam(user_id=USER, skill_id="test"))
    await db.add(EmergencyCancel(user_id=USER))

    await db.add(_rating("rating-lecturer", USER, None))
    await db.add(_rating("rating-participant", OTHER, USER))
    await db.add(_rating("rating-other", OTHER, OTHER))

    await CalendarToken.get_or_create(USER)
    await CalendarToken.get_or_create(OTHER)


@pytest.fixture
async def past_data(session: AsyncSession) -> None:
    """Ended availability alone establishes neither performance nor loss of rights."""

    webinar = _webinar("webinar-past", USER)
    webinar.start = utcnow() - timedelta(hours=2)
    webinar.end = utcnow() - timedelta(hours=1)
    await db.add(webinar)
    await db.add(paid_participant(webinar_id="webinar-past", user_id=OTHER, paid_coins=1337))

    slot = _slot("slot-past-booked", USER, OTHER)
    slot.start = utcnow() - timedelta(hours=2)
    slot.end = utcnow() - timedelta(hours=1)
    await db.add(slot)


async def _all(cls: Any) -> list[Any]:
    return await db.all(select(cls))


async def test__delete_user_data__removes_unneeded_data_preserves_bookings(data: None) -> None:
    await delete_user_data(USER)

    assert sorted(w.id for w in await _all(Webinar)) == ["webinar-other", "webinar-third", "webinar-user"]
    assert {(p.webinar_id, p.user_id) for p in await _all(WebinarParticipant)} == {
        ("webinar-user", OTHER),
        ("webinar-other", USER),
        ("webinar-third", USER),
        ("webinar-third", OTHER),
    }
    assert sorted(s.id for s in await _all(Slot)) == ["slot-other", "slot-other-booked", "slot-user-booked"]
    assert [w.id for w in await _all(WeeklySlot)] == ["weekly-other"]
    assert [c.user_id for c in await _all(Coaching)] == []
    assert [e.user_id for e in await _all(Exam)] == []
    assert [e.user_id for e in await _all(EmergencyCancel)] == []
    assert [r.id for r in await _all(LecturerRating)] == ["rating-other"]
    assert [t.user_id for t in await _all(CalendarToken)] == [OTHER]
    assert len(await _all(BookingPayment)) == 6
    rights = await _all(RetainedEventRight)
    assert {(r.event_id, r.role) for r in rights} == {
        ("webinar-user", "instructor"),
        ("slot-user-booked", "instructor"),
        ("webinar-other", "participant"),
        ("webinar-third", "participant"),
        ("slot-other-booked", "participant"),
    }
    assert all(r.source_subject == USER and r.current_subject is None for r in rights)
    assert all(r.original["payment_or_performance_inferred"] is False for r in rights)
    assert await _all(SettlementClaim) == []
    webinar = await db.get(Webinar, id="webinar-user")
    assert webinar is not None and webinar.closed_to_new_bookings is True


async def test__delete_user_data__preserves_booked_slot_terms(data: None) -> None:
    payments = {p.id: (p.user_id, p.paid_coins, p.payout_coins) for p in await _all(BookingPayment)}
    await delete_user_data(USER)

    slot = await db.get(Slot, id="slot-other-booked")
    assert slot is not None
    assert slot.user_id == OTHER
    assert slot.booked_by == USER
    assert slot.event_type == EventType.COACHING
    assert slot.student_coins == 42
    assert slot.instructor_coins == 21
    assert slot.skill_id == "test"
    assert slot.admin_link == "https://meet.jit.si/admin"
    assert slot.link == "https://meet.jit.si/link"
    assert {p.id: (p.user_id, p.paid_coins, p.payout_coins) for p in await _all(BookingPayment)} == payments


async def test__delete_user_data__keeps_other_users(data: None) -> None:
    await delete_user_data(OTHER)

    assert sorted(w.id for w in await _all(Webinar)) == ["webinar-other", "webinar-third", "webinar-user"]
    assert len(await _all(WebinarParticipant)) == 4
    assert sorted(s.id for s in await _all(Slot)) == ["slot-other-booked", "slot-user", "slot-user-booked"]
    assert [w.id for w in await _all(WeeklySlot)] == ["weekly-user"]
    assert [c.user_id for c in await _all(Coaching)] == [USER]
    assert [e.user_id for e in await _all(Exam)] == [USER]
    assert [e.user_id for e in await _all(EmergencyCancel)] == [USER]
    assert [r.id for r in await _all(LecturerRating)] == ["rating-lecturer"]
    assert [t.user_id for t in await _all(CalendarToken)] == [USER]

    slot = await db.get(Slot, id="slot-user-booked")
    assert slot is not None and slot.booked_by == OTHER
    assert all(r.source_subject == OTHER for r in await _all(RetainedEventRight))
    assert await db.get(EventSubjectGuard, subject=USER) is None


async def test__delete_user_data__unknown_user(data: None) -> None:
    await delete_user_data(UNKNOWN)

    assert len(await _all(Webinar)) == 3
    assert len(await _all(WebinarParticipant)) == 4
    assert len(await _all(Slot)) == 4
    assert len(await _all(WeeklySlot)) == 2
    assert len(await _all(Coaching)) == 1
    assert len(await _all(Exam)) == 1
    assert len(await _all(EmergencyCancel)) == 1
    assert len(await _all(LecturerRating)) == 3
    assert len(await _all(CalendarToken)) == 2
    assert await _all(RetainedEventRight) == []
    assert await _all(SettlementClaim) == []


async def test__delete_user_data__idempotent(data: None, commercial_remote: CommercialResponses) -> None:
    await delete_user_data(USER)
    rights = {r.id: deepcopy(r.original) for r in await _all(RetainedEventRight)}
    observations = {r.id for r in await _all(RetainedEventErasure)}
    receipt = await db.get(CommercialErasureReceipt, subject=USER)
    assert receipt is not None
    stamps = (receipt.observed_at, receipt.erased_at, receipt.acknowledged_at)
    await delete_user_data(USER)

    assert len(rights) == 5
    assert {r.id: r.original for r in await _all(RetainedEventRight)} == rights
    assert {r.id for r in await _all(RetainedEventErasure)} == observations
    assert (receipt.observed_at, receipt.erased_at, receipt.acknowledged_at) == stamps
    assert len(await _all(SettlementBatch)) == 1
    assert len(commercial_remote.bodies("inventory")) == 2
    assert commercial_remote.bodies("inventory")[0] == commercial_remote.bodies("inventory")[1]


async def test__delete_user_data__clears_cache(data: None, clear_cache_patch: AsyncMock) -> None:
    await delete_user_data(USER)

    assert clear_cache_patch.call_args_list == [call(prefix) for prefix in USER_CACHE_PREFIXES]


async def test__delete_user_data__preserves_counterparty_rights_without_cancellation(
    data: None, commercial_remote: CommercialResponses
) -> None:
    await delete_user_data(USER)

    payments = {p.event_id: p for p in await _all(BookingPayment) if p.user_id == OTHER}
    assert payments["webinar-user"].paid_coins == 1337
    assert payments["slot-user-booked"].paid_coins == 42
    assert await db.get(WebinarParticipant, webinar_id="webinar-user", user_id=OTHER) is not None
    assert await _all(SettlementClaim) == []
    assert await _all(CoinOperation) == []
    assert commercial_remote.bodies("register_event") == []


async def test__delete_user_data__preserves_deleted_users_financial_ownership(data: None) -> None:
    await delete_user_data(USER)

    payments = {p.id: p for p in await _all(BookingPayment) if p.user_id == USER}
    assert sorted(p.paid_coins for p in payments.values()) == [42, 1337, 1337]
    rights = [r for r in await _all(RetainedEventRight) if r.role == "participant"]
    assert {r.payment_id for r in rights} == set(payments)
    assert all(r.source_subject == USER and r.current_subject is None for r in rights)
    assert await _all(SettlementClaim) == []


async def test__identified_provider_cancellation__preserves_original_zero_and_positive_claims(
    session: AsyncSession, commercial_remote: CommercialResponses
) -> None:
    webinar = _webinar("webinar-user", USER)
    await db.add(webinar)
    await db.add(paid_participant(webinar_id="webinar-user", user_id=OTHER, paid_coins=0))
    await db.add(paid_participant(webinar_id="webinar-user", user_id=THIRD, paid_coins=42))
    payments = {p.user_id: p for p in await _all(BookingPayment)}
    declared = webinar.start - timedelta(hours=1)
    commercial_remote.receipts[USER] = canonical_erasure(
        USER,
        {
            "paid_contract_intent": "cancel_identified_contracts",
            "contract_ids": [p.id for p in payments.values()],
            "original_text": "Cancel these two identified provider bookings.",
            "received_at": declared.isoformat(),
        },
    )

    await delete_user_data(USER)

    claims = await _all(SettlementClaim)
    assert len(claims) == 4
    student = {c.user_id: c for c in claims if c.amount_field == "paid_coins"}
    assert {owner: c.coins for owner, c in student.items()} == {OTHER: 0, THIRD: 42}
    assert all(c.entitlement == "established" and c.resolved_at is not None for c in student.values())
    assert all(c.payment_ids == [payments[owner].id] for owner, c in student.items())
    assert all(c.basis["request_received_at"] == declared.isoformat() for c in student.values())
    instructor = [c for c in claims if c.amount_field == "payout_coins"]
    assert len(instructor) == 2 and {c.user_id for c in instructor} == {USER}
    assert all(c.coins is None and c.resolved_at is None and c.entitlement == "pending_evidence" for c in instructor)
    operations = await _all(CoinOperation)
    assert len(operations) == 1
    assert (operations[0].id, operations[0].user_id, operations[0].coins, operations[0].completed_at) == (
        student[THIRD].id,
        THIRD,
        42,
        None,
    )
    handoffs = await _all(CommercialHandoff)
    assert len(handoffs) == 4 and all(h.acknowledged_at is not None for h in handoffs)
    assert all(h.receipt["disposition"] == "claim_preserved" for h in handoffs)
    assert await _all(WebinarParticipant) == []
    original_ids = {c.id for c in claims}
    await delete_user_data(USER)
    assert {c.id for c in await _all(SettlementClaim)} == original_ids
    assert len(commercial_remote.bodies("register_event")) == 4


async def test__delete_user_data__past_time_does_not_prove_performance_or_forfeiture(past_data: None) -> None:
    await delete_user_data(USER)

    rights = await _all(RetainedEventRight)
    assert len(rights) == 2 and all(r.state == "preserved" for r in rights)
    assert all(r.original["payment_or_performance_inferred"] is False for r in rights)
    assert len(await _all(WebinarParticipant)) == 1 and len(await _all(Slot)) == 1
    assert await _all(SettlementClaim) == []


async def test__delete_user_data__zero_student_payment_keeps_distinct_instructor_amount(session: AsyncSession) -> None:
    webinar = _webinar("webinar-free", USER)
    webinar.price = 0
    await db.add(webinar)
    await db.add(paid_participant(webinar_id="webinar-free", user_id=OTHER, paid_coins=0))
    slot = _slot("slot-free-booked", USER, OTHER)
    slot.student_coins = 0
    payment = await db.get(BookingPayment, id=slot.payment_id)
    assert payment is not None
    payment.paid_coins = 0
    await db.add(slot)

    await delete_user_data(USER)

    assert payment.paid_coins == 0 and payment.payout_coins == 21
    assert len(await _all(RetainedEventRight)) == 2
    assert {p.paid_coins for p in await _all(BookingPayment)} == {0}
    assert await _all(SettlementClaim) == []
    assert await _all(CoinOperation) == []


@pytest.mark.parametrize("unavailable", [False, True])
async def test__delete_user_data__canonical_absence_preserves_commit_then_recovers(
    data: None, commercial_remote: CommercialResponses, unavailable: bool
) -> None:
    canonical = commercial_remote.receipts.pop(USER)
    if unavailable:
        commercial_remote.unavailable.add(USER)
    with pytest.raises(HTTPException) as error:
        await delete_user_data(USER)
    assert error.value.status_code == 503
    detail: object = error.value.detail
    assert detail == {"code": "CommercialInventoryPending", "erasure_committed": True}
    await db.session.rollback()  # A reported failure must not undo the local erasure commit.
    receipt = await db.get(CommercialErasureReceipt, subject=USER)
    assert receipt is not None and receipt.erased_at is not None
    assert receipt.canonical is None and receipt.acknowledged_at is None
    erased_at = receipt.erased_at
    rights = {r.id: deepcopy(r.original) for r in await _all(RetainedEventRight)}
    observations = {r.id for r in await _all(RetainedEventErasure)}
    assert len(rights) == 5 and len(observations) == 5
    assert len(await _all(BookingPayment)) == 6 and len(await _all(WebinarParticipant)) == 4
    assert await db.get(CalendarToken, user_id=USER) is None
    assert commercial_remote.bodies("inventory") == []
    commercial_remote.unavailable.clear()
    commercial_remote.receipts[USER] = canonical
    await delete_user_data(USER)
    assert receipt.erased_at == erased_at and receipt.canonical == canonical and receipt.acknowledged_at is not None
    assert {r.id: r.original for r in await _all(RetainedEventRight)} == rights
    recovered = await _all(RetainedEventErasure)
    assert len(recovered) == 10 and observations < {r.id for r in recovered}
    assert all(r.receipt["paid_cancellation_inferred"] is False for r in recovered)
    assert await _all(SettlementClaim) == []


@pytest.mark.parametrize("failure", ["lost", "financial_satisfaction"])
async def test__delete_user_data__inventory_failure_retries_same_evidence(
    data: None, commercial_remote: CommercialResponses, failure: str
) -> None:
    commercial_remote.inventory_failure = failure
    with pytest.raises(RuntimeError if failure == "lost" else ValueError):
        await delete_user_data(USER)
    await db.session.rollback()
    receipt = await db.get(CommercialErasureReceipt, subject=USER)
    assert receipt is not None and receipt.erased_at is not None and receipt.acknowledged_at is None
    right_ids = {r.id for r in await _all(RetainedEventRight)}
    payload = commercial_remote.bodies("inventory")[0]
    commercial_remote.inventory_failure = None
    await delete_user_data(USER)
    assert receipt.acknowledged_at is not None
    assert commercial_remote.bodies("inventory") == [payload, payload]
    assert {r.id for r in await _all(RetainedEventRight)} == right_ids
    assert await _all(CoinOperation) == []


async def test__delete_user_data__missing_payment_is_unknown_not_zero(session: AsyncSession) -> None:
    await db.add(_webinar("legacy-webinar", OTHER))
    booked = await db.add(WebinarParticipant(webinar_id="legacy-webinar", user_id=USER, paid_coins=99))
    await delete_user_data(USER)
    payment = await db.get(BookingPayment, id=booked.payment_id)
    assert payment is not None and payment.state == "legacy_unknown"
    assert payment.paid_coins is None and payment.payout_coins is None and payment.quoted_coins is None
    assert payment.original["asserted_student_coins"] == 99
    rights = await _all(RetainedEventRight)
    assert len(rights) == 1 and rights[0].original["paid_coins"] is None
    payment_id = payment.id
    await delete_user_data(USER)
    assert booked.payment_id == payment_id and len(await _all(BookingPayment)) == 1
    assert await _all(SettlementClaim) == []
