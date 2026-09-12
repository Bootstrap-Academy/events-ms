from copy import deepcopy
from datetime import timedelta
from typing import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from api.database import db, db_context, select
from api.models import BookingPayment, EmergencyCancel, Webinar
from api.models.benefit import EventBenefit
from api.models.booking_payment import CommercialHandoff, SettlementClaim
from api.models.settlement import CoinOperation
from api.models.webinars import clean_old_webinars
from api.services.settlements import recover_settlements
from api.settings import settings
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant
from tests.services.test_user_deletion import CommercialResponses


LECTURER = "9f4e2d17-9e2b-4b02-8c0f-3a8c07c5f4f0"
STUDENT = "c1d2eb59-8b1a-4a2f-9c37-1b8e5d7f60a3"
OTHER = "b5a6b0c2-0f39-4a3c-9a3a-2d8d3d9d4a11"

PRICE = 1000
AGREED_PAYOUT = 700


@pytest.fixture(autouse=True)
def commercial_remote(mocker: MockerFixture) -> Iterator[CommercialResponses]:
    # Successful JSON-null erasure responses select normal cleanup. Outages
    # and actual declarations are separate controls, never unconditional success.
    remote = CommercialResponses()
    mocker.patch("api.services.shop.commercial", side_effect=remote.__call__)
    legacy = mocker.patch("api.services.shop.apply_coin_operation", AsyncMock())
    yield remote
    assert remote.unexpected == []
    legacy.assert_not_awaited()


async def _past_webinar(participants: list[tuple[str, int, int | None]]) -> dict[str, str]:
    payment_ids = {}
    async with db_context():
        await db.add(
            Webinar(
                id="webinar",
                skill_id="test",
                creator=LECTURER,
                creation_date=utcnow(),
                name="test webinar",
                description="test description",
                admin_link="https://meet.jit.si/admin",
                link="https://meet.jit.si/link",
                start=utcnow() - timedelta(hours=2),
                end=utcnow() - timedelta(hours=1),
                max_participants=42,
                price=1337,  # Current advertised price is not the original payment or payout.
            )
        )
        for user_id, paid_coins, agreed_payout in participants:
            booked = await db.add(paid_participant(webinar_id="webinar", user_id=user_id, paid_coins=paid_coins))
            payment = await db.get(BookingPayment, id=booked.payment_id)
            assert payment is not None
            payment.payout_coins = agreed_payout
            payment.original = {"synthetic_original_agreed_payout": agreed_payout}
            payment_ids[user_id] = payment.id
    return payment_ids


async def test__clean_old_webinars__hands_off_original_agreed_remuneration(
    database: None, commercial_remote: CommercialResponses, mocker: MockerFixture
) -> None:
    payment_ids = await _past_webinar([(STUDENT, PRICE, AGREED_PAYOUT), (OTHER, PRICE, AGREED_PAYOUT)])
    mocker.patch.object(settings, "event_fee", 0.8)

    await clean_old_webinars()

    async with db_context():
        assert await db.all(select(Webinar)) == []
        claims = await db.all(select(SettlementClaim))
        assert len(claims) == 2 and all(c.user_id == LECTURER for c in claims)
        assert {tuple(c.payment_ids) for c in claims} == {(payment_id,) for payment_id in payment_ids.values()}
        assert all(c.amount_field == "payout_coins" and c.coins == AGREED_PAYOUT for c in claims)
        assert all(c.entitlement == "established" and c.resolved_at is not None for c in claims)
        operations = await db.all(select(CoinOperation))
        assert {op.id for op in operations} == {c.id for c in claims}
        assert sum(op.coins for op in operations) == 2 * AGREED_PAYOUT
        assert all(op.user_id == LECTURER and op.completed_at is None and op.attempts == 0 for op in operations)
        handoffs = await db.all(select(CommercialHandoff))
        assert len(handoffs) == 2 and all(h.acknowledged_at is not None for h in handoffs)
    payloads = commercial_remote.bodies("register_event")
    assert len(payloads) == 2
    for payload in payloads:
        assert payload["subject"] == LECTURER
        assert payload["operation_id"] == payload["obligation_id"] == payload["identity"]["source_key"]
        assert payload["identity"]["component"] == "instructor_remuneration"
        assert payload["identity"]["amount_field"] == "payout_coins"
        assert payload["observation"]["units"] == AGREED_PAYOUT
        assert payload["observation"]["payments"][0]["paid_coins"] == PRICE
        assert payload["observation"]["payments"][0]["payout_coins"] == AGREED_PAYOUT
        assert payload["operation_payload"] == {"coins": AGREED_PAYOUT, "description": "Webinar", "credit_note": True}


async def test__clean_old_webinars__keeps_explicit_zero_and_positive_remuneration(
    database: None, commercial_remote: CommercialResponses
) -> None:
    # This fixture expressly records zero remuneration for the free booking.
    # A zero payer amount alone would not establish that separate amount.
    payment_ids = await _past_webinar([(STUDENT, 0, 0), (OTHER, PRICE, AGREED_PAYOUT)])

    await clean_old_webinars()

    async with db_context():
        claims = await db.all(select(SettlementClaim))
        assert {tuple(c.payment_ids): c.coins for c in claims} == {
            (payment_ids[STUDENT],): 0,
            (payment_ids[OTHER],): AGREED_PAYOUT,
        }
        assert all(c.resolved_at is not None for c in claims)
        operations = await db.all(select(CoinOperation))
        assert len(operations) == 1 and operations[0].coins == AGREED_PAYOUT
        assert operations[0].completed_at is None
    payloads = commercial_remote.bodies("register_event")
    zero = next(p for p in payloads if p["identity"]["payment_ids"] == [payment_ids[STUDENT]])
    assert zero["observation"]["units"] == 0 and "operation_id" not in zero
    assert len(payloads) == 2


async def test__clean_old_webinars__settles_the_emergency_cancellation(database: None) -> None:
    async with db_context():
        await EmergencyCancel.create(LECTURER)
    await _past_webinar([(STUDENT, PRICE, AGREED_PAYOUT)])

    await clean_old_webinars()

    async with db_context():
        assert await EmergencyCancel.exists(LECTURER) is False


async def test__clean_old_webinars__acknowledged_unknown_remuneration_is_not_paid(
    database: None, commercial_remote: CommercialResponses
) -> None:
    payment_ids = await _past_webinar([(STUDENT, PRICE, None)])
    await clean_old_webinars()
    async with db_context():
        assert await db.all(select(Webinar)) == []
        claims = await db.all(select(SettlementClaim))
        assert len(claims) == 1
        claim = claims[0]
        assert claim.user_id == LECTURER and claim.payment_ids == [payment_ids[STUDENT]]
        assert claim.coins is None and claim.resolved_at is None
        assert await db.all(select(CoinOperation)) == []
        saved = await db.get(CommercialHandoff, claim_id=claim.id)
        assert saved is not None and saved.acknowledged_at is not None
        assert saved.receipt == {"protocol": 1, "obligation_id": claim.id, "disposition": "claim_preserved"}
    payload = commercial_remote.bodies("register_event")[0]
    assert payload["observation"]["units"] is None and payload["observation"]["computed_units"] is None
    assert payload["observation"]["payments"][0]["paid_coins"] == PRICE
    assert payload["observation"]["payments"][0]["payout_coins"] is None
    assert "operation_id" not in payload


async def test__clean_old_webinars__unavailable_erasure_evidence_keeps_two_pending_components(
    database: None, commercial_remote: CommercialResponses
) -> None:
    payment_ids = await _past_webinar([(STUDENT, PRICE, AGREED_PAYOUT)])
    async with db_context():
        await EmergencyCancel.create(LECTURER)
    commercial_remote.unavailable.add(STUDENT)
    await clean_old_webinars()
    async with db_context():
        assert await db.all(select(Webinar)) == []
        claims = await db.all(select(SettlementClaim))
        assert {(c.user_id, c.amount_field, c.coins) for c in claims} == {
            (STUDENT, "paid_coins", PRICE),
            (LECTURER, "payout_coins", AGREED_PAYOUT),
        }
        assert all(c.payment_ids == [payment_ids[STUDENT]] and c.entitlement == "pending_evidence" for c in claims)
        assert all(c.basis["backend_temporarily_unavailable"] is True for c in claims)
        assert all(c.basis["cancellation_inferred_from_erasure"] is False for c in claims)
        assert await db.all(select(EventBenefit)) == []
        assert await EmergencyCancel.exists(LECTURER) is True
        assert all(op.completed_at is None for op in await db.all(select(CoinOperation)))
    payloads = commercial_remote.bodies("register_event")
    assert len(payloads) == 2 and all(p["observation"]["units"] is None for p in payloads)
    assert {p["observation"]["computed_units"] for p in payloads} == {PRICE, AGREED_PAYOUT}


@pytest.mark.parametrize("failure", ["lost", "identity", "disposition"])
async def test__clean_old_webinars__failed_handoff_recovers_exact_committed_claim(
    database: None, commercial_remote: CommercialResponses, failure: str
) -> None:
    payment_ids = await _past_webinar([(STUDENT, PRICE, AGREED_PAYOUT)])
    commercial_remote.handoff_failure = failure
    with pytest.raises(HTTPException) as error:
        await clean_old_webinars()
    assert error.value.status_code == 503
    detail: object = error.value.detail
    assert detail == {
        "code": "EventSettlementPending",
        "cancellation_committed": True,
        "pending_operations": 1,
        "reconciliation_may_be_required": True,
    }
    payload = deepcopy(commercial_remote.bodies("register_event")[0])
    async with db_context():
        assert await db.all(select(Webinar)) == []
        assert await db.get(BookingPayment, id=payment_ids[STUDENT]) is not None
        claims = await db.all(select(SettlementClaim))
        assert len(claims) == 1
        claim_id = claims[0].id
        saved = await db.get(CommercialHandoff, claim_id=claim_id)
        assert saved is not None and saved.payload == payload
        assert saved.acknowledged_at is None and saved.receipt is None and saved.attempts == 1
        assert saved.last_error == ("RuntimeError" if failure == "lost" else "ValueError")
    commercial_remote.handoff_failure = None
    await recover_settlements()
    await recover_settlements()
    assert commercial_remote.bodies("register_event") == [payload, payload]
    assert len(commercial_remote.registered) == 1
    async with db_context():
        assert {c.id for c in await db.all(select(SettlementClaim))} == {claim_id}
        saved = await db.get(CommercialHandoff, claim_id=claim_id)
        assert saved is not None and saved.payload == payload and saved.attempts == 2
        assert saved.acknowledged_at is not None and saved.last_error is None
        operations = await db.all(select(CoinOperation))
        assert len(operations) == 1 and operations[0].id == claim_id
        assert operations[0].coins == AGREED_PAYOUT and operations[0].completed_at is None
        assert operations[0].attempts == 0
