"""Prospective owning cleanup/outbox transactions; committed destination is a stub."""

from datetime import timedelta
from typing import Any, Literal
from uuid import uuid4

import httpx
import pytest
from pytest_mock import MockerFixture
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, db_context, filter_by, select
from api.models import EventBenefit, EventBenefitObservation, Slot, Webinar
from api.models.webinars import clean_old_webinars
from api.services import benefits, skills
from api.services.internal import InternalService
from api.services.user_export import export_user_data
from api.utils.utc import utcnow
from tests.payment_fixtures import paid_participant
from tests.required import required, unwrapped
from tests.services import test_retained_events
from tests.services.test_retained_events import booking
from tests.services.test_user_deletion import OTHER, THIRD, USER, _slot


async def qualify(
    remote: dict[str, Any],
    mocker: MockerFixture,
    *,
    kind: Literal["webinar", "coaching"] = "webinar",
    two: bool = False,
) -> Webinar | Slot:
    event: Webinar | Slot
    if kind == "webinar":
        event, booked = await booking(days=-1)
        if two:
            await db.add(paid_participant(webinar_id=event.id, user_id=THIRD, paid_coins=77))
        # Explicit synthetic newly created session/bookings, not migration backfill.
        event.xp_delivery_protocol = 1
        from api.models import BookingPayment

        for payment in await db.all(filter_by(BookingPayment, event_id=event.id)):
            payment.xp_delivery_protocol = 1
        await unwrapped(clean_old_webinars)()
    else:
        from api.models.slots import clean_old_slots

        event = _slot(str(uuid4()), OTHER, USER)
        event.start = utcnow() - timedelta(hours=2)
        event.end = utcnow() - timedelta(hours=1)
        await db.add(event)
        from api.models import BookingPayment

        (required(await db.get(BookingPayment, id=event.payment_id))).xp_delivery_protocol = 1
        await unwrapped(clean_old_slots)()
    return event


@pytest.mark.parametrize("kind", ["webinar", "coaching"])
async def test_cleanup_and_exact_configured_earnings_commit_together(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture, kind: Literal["webinar", "coaching"]
) -> None:
    from api.settings import settings

    await qualify(remote, mocker, kind=kind, two=kind == "webinar")
    await db.commit()
    rows = await db.all(select(EventBenefit))
    assert len(rows) == (3 if kind == "webinar" else 2)
    assert len([r for r in rows if r.role == "instructor"]) == 1
    for row in rows:
        assert row.request["xp"] == getattr(
            settings, f'{kind}_{"lecturer" if row.role=="instructor" else "participant"}_xp'
        )
        assert row.request["user_id"] == row.user_id and row.state == "pending" and row.attempts == 0
        assert row.original["attendance_or_payment_proof_inferred"] is False
    assert (await export_user_data(str(uuid4()))).event_benefits == {"earnings": [], "observations": []}
    assert len((await export_user_data(OTHER)).event_benefits["earnings"]) == 1


async def test_cleanup_rollback_does_not_create_earning_or_dispatch(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture
) -> None:
    event, _ = await booking(days=-1)
    event_id = event.id
    await db.commit()
    actual = db.delete

    async def fail_parent(row: Any) -> Any:
        if isinstance(row, Webinar):
            raise RuntimeError("Synthetic producer transaction failure")
        return await actual(row)

    mocker.patch.object(db, "delete", side_effect=fail_parent)
    with pytest.raises(RuntimeError):
        await unwrapped(clean_old_webinars)()
    await db.session.rollback()
    assert await db.all(select(EventBenefit)) == [] and await db.get(Webinar, id=event_id) is not None


async def test_retry_freezes_identity_and_config_and_reply_loss_cannot_duplicate_effect(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture
) -> None:
    await qualify(remote, mocker)
    await db.commit()
    rows = await db.all(select(EventBenefit))
    original = {r.id: dict(r.request) for r in rows}
    effects: dict[str, Any] = {}
    lost = set()

    async def destination(operation: str, request: Any) -> Any:
        if operation in effects:
            assert effects[operation] == request
        else:
            effects[operation] = dict(request)
        if operation not in lost:
            lost.add(operation)
            raise RuntimeError("Synthetic remote commit followed by lost reply")
        return {"operation_id": operation, "request": request, "state": "applied", "applied": True}

    mocker.patch("api.services.skills.apply_xp_benefit", side_effect=destination)
    await benefits.recover()
    async with db_context():
        rows = await db.all(select(EventBenefit))
        assert all(r.state == "uncertain" and r.attempts == 1 for r in rows)
        assert {r.id: r.request for r in rows} == original
        await db.exec(update(EventBenefit).values(next_attempt_at=utcnow() - timedelta(seconds=1)))
    await benefits.recover()
    async with db_context():
        assert all(r.state == "applied" and r.attempts == 2 for r in await db.all(select(EventBenefit)))
        assert len(await db.all(select(EventBenefitObservation))) == 4
        assert len(effects) == 2
        before = await benefits.export(USER)
    await benefits.recover()
    async with db_context():
        assert await benefits.export(USER) == before


async def test_acknowledgment_rollback_retries_exact_already_committed_remote_effect(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture
) -> None:
    await qualify(remote, mocker)
    await db.commit()
    effects: dict[str, Any] = {}

    async def destination(operation: str, request: Any) -> Any:
        effects.setdefault(operation, dict(request))
        assert effects[operation] == request
        return {"operation_id": operation, "request": request, "state": "applied", "applied": True}

    mocker.patch("api.services.skills.apply_xp_benefit", side_effect=destination)
    actual = db.commit
    first = True

    async def fail_once() -> None:
        nonlocal first
        if first:
            first = False
            raise RuntimeError("Synthetic acknowledgment transaction failure")
        await actual()

    mocker.patch.object(db, "commit", side_effect=fail_once)
    with pytest.raises(RuntimeError):
        await benefits.dispatch_one()
    async with db_context():
        assert all(r.state == "pending" and r.attempts == 0 for r in await db.all(select(EventBenefit)))
        assert await db.all(select(EventBenefitObservation)) == []
    assert await benefits.dispatch_one()
    async with db_context():
        assert len([r for r in await db.all(select(EventBenefit)) if r.state == "applied"]) == 1
    assert len(effects) == 1


async def test_repeated_earning_keeps_first_beneficiary_and_amount(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture
) -> None:
    event, booked = await booking()
    from api.models import BookingPayment
    from api.services import commercial

    payment = required(await db.get(BookingPayment, id=booked.payment_id))
    await commercial.retain_event(payment, event, OTHER)
    first = await benefits.record(event, payment, "participant", USER, 17)
    await db.commit()
    original = dict((required(await db.get(EventBenefit, id=first))).request)
    assert await benefits.record(event, payment, "participant", THIRD, 999) == first
    await db.commit()
    row = required(await db.get(EventBenefit, id=first))
    assert row.request == original and row.user_id == USER


@pytest.mark.parametrize(
    "response_kind",
    ["applied", "erased", "404", "422", "503", "conflict", "wrong_operation", "wrong_subject", "bool_xp", "malformed"],
)
async def test_transport_requires_exact_typed_receipt(mocker: MockerFixture, response_kind: str) -> None:
    operation, user, earning = [str(uuid4()) for _ in range(3)]
    request = {"user_id": user, "skill_id": "ordinary skill", "xp": 1, "earning_id": earning}
    result: dict[str, Any] = {"operation_id": operation, "request": dict(request), "state": "applied", "applied": True}
    status = 200
    if response_kind in {"404", "422", "503"}:
        status = int(response_kind)
    if response_kind == "conflict":
        status = 409
    if response_kind == "erased":
        result.update(state="recipient_erased", applied=False)
    if response_kind == "wrong_operation":
        result["operation_id"] = str(uuid4())
    if response_kind == "wrong_subject":
        result["request"]["user_id"] = str(uuid4())
    if response_kind == "bool_xp":
        result["request"]["xp"] = True
    observed = []

    def transport(req: Any) -> Any:
        observed.append(req)
        return (
            httpx.Response(status, json=result)
            if response_kind != "malformed"
            else httpx.Response(200, text="not json")
        )

    mocker.patch.object(
        InternalService,
        "client",
        property(
            lambda self: httpx.AsyncClient(
                base_url="http://fixture.invalid/_internal", transport=httpx.MockTransport(transport)
            )
        ),
    )
    outcome = await skills.apply_xp_benefit(operation, request)
    expected = (
        "applied"
        if response_kind == "applied"
        else (
            "recipient_erased"
            if response_kind == "erased"
            else "review" if response_kind == "conflict" else "uncertain"
        )
    )
    assert outcome["state"] == expected
    assert len(observed) == 1 and observed[0].url.path == f"/_internal/xp-operations/{operation}/{user}/ordinary skill"


remote = test_retained_events.remote
