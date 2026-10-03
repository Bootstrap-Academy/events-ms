"""Exact elected host rights govern access independently of sibling UUID order."""

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import db, filter_by
from api.endpoints.learning import existing_events
from api.models import BookingContract, BookingPayment, EventRightGrant, RetainedEventRight, Webinar, WebinarParticipant
from api.services import retained_events
from api.services.user_deletion import delete_user_data
from tests.payment_fixtures import paid_participant
from tests.required import required
from tests.services import test_retained_events
from tests.services.test_retained_events import booking, canonical
from tests.services.test_user_deletion import OTHER, THIRD, USER


async def two_rights(remote: dict[str, Any]) -> tuple[Webinar, list[RetainedEventRight]]:
    event, _ = await booking(student=OTHER, provider=USER, days=0)
    await db.add(paid_participant(webinar_id=event.id, user_id=str(uuid4()), paid_coins=77))
    remote[USER] = canonical(USER)
    await delete_user_data(USER)
    rights = await db.all(filter_by(RetainedEventRight, event_id=event.id).order_by(RetainedEventRight.id))
    assert len(rights) == 2
    return event, rights


async def elect(mocker: MockerFixture, right: RetainedEventRight) -> str:
    grant = str(uuid4())
    authority = {
        "id": grant,
        "source": "events",
        "purpose": "existing_event_continuation",
        "new_purchase": False,
        "successor": THIRD,
        "original_contract": right.id,
        "original_scope": await retained_events.get_original(USER, right.id),
        "claimant_authorization": {"source_subject": USER},
    }
    mocker.patch("api.services.retained_events.successor_authority", AsyncMock(return_value=authority))
    assert (await retained_events.deliver(USER, grant))["state"] == "granted"
    return grant


async def unavailable(right: RetainedEventRight, state: str) -> None:
    payment = required(await db.get(BookingPayment, id=right.payment_id))
    await db.add(
        BookingContract(
            id=payment.id,
            user_id=payment.user_id,
            event_id=payment.event_id,
            kind=payment.kind,
            offer={"product": {"facts": {}}},
            state="ready" if state == "closed" else state,
            closed=state == "closed",
            reported=False,
        )
    )


@pytest.mark.parametrize("unready_index", [0, 1])
@pytest.mark.parametrize("state", ["prepared", "review", "closed"])
async def test_only_exact_elected_host_grant_controls_link(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture, unready_index: int, state: str
) -> None:
    event, rights = await two_rights(remote)
    original_owners = {r.payment_id: (required(await db.get(BookingPayment, id=r.payment_id))).user_id for r in rights}
    await unavailable(rights[unready_index], state)
    elected = rights[1 - unready_index]
    grant = await elect(mocker, elected)
    await db.commit()
    view = await existing_events(THIRD, event.id)
    assert len(view) == 1 and view[0]["state"] == "available" and view[0]["right_id"] == elected.id
    assert view[0]["link"] == event.admin_link and event.closed_to_new_bookings is True
    assert len(await db.all(filter_by(WebinarParticipant, webinar_id=event.id))) == 2
    assert {
        r.payment_id: (required(await db.get(BookingPayment, id=r.payment_id))).user_id for r in rights
    } == original_owners
    remote[THIRD] = canonical(THIRD)
    await delete_user_data(THIRD)
    assert await existing_events(THIRD, event.id) == []
    assert (required(await db.get(EventRightGrant, id=grant))).state == "withdrawn"


async def test_second_exact_grant_can_remain_usable_after_first_requires_resolution(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture
) -> None:
    event, rights = await two_rights(remote)
    for right in rights:
        await elect(mocker, right)
    await unavailable(rights[0], "review")
    await db.commit()
    view = await existing_events(THIRD, event.id)
    assert len(view) == 1 and view[0]["state"] == "available" and view[0]["right_id"] == rights[1].id
    await unavailable(rights[1], "closed")
    await db.commit()
    view = await existing_events(THIRD, event.id)
    assert len(view) == 1 and view[0]["state"] == "resolution_required" and view[0]["link"] is None


async def test_ready_sibling_without_election_cannot_replace_unavailable_exact_grant(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture
) -> None:
    event, rights = await two_rights(remote)
    await elect(mocker, rights[1])
    await unavailable(rights[1], "review")
    await db.commit()
    view = await existing_events(THIRD, event.id)
    assert len(view) == 1 and view[0]["state"] == "resolution_required" and view[0]["link"] is None


@pytest.mark.parametrize("route", ["calendar", "detail"])
@pytest.mark.parametrize("elected_index", [0, 1])
@pytest.mark.parametrize("state", ["review", "closed", "withdrawn"])
async def test_all_scoped_webinar_views_use_exact_host_election(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture, elected_index: int, state: str, route: str
) -> None:
    from uuid import UUID

    from api.endpoints import learning
    from api.schemas.user import User, UserInfo

    info = UserInfo(id=THIRD, name="synthetic", display_name="Synthetic host", avatar_url=None)
    for path in ("api.endpoints.calendar.get_userinfo", "api.models.webinars.get_userinfo"):
        mocker.patch(path, AsyncMock(return_value=info))
    mocker.patch("api.models.webinars.LecturerRating.get_rating", AsyncMock(return_value=None))
    event, rights = await two_rights(remote)
    grant_id = await elect(mocker, rights[elected_index])
    await db.commit()
    user = User(id=THIRD, email_verified=True, admin=False)
    original = {r.payment_id: (required(await db.get(BookingPayment, id=r.payment_id))).user_id for r in rights}

    async def assert_links(expected: str | None) -> None:
        access = await learning.existing_events(THIRD, event.id)
        assert (access[0]["link"] if access else None) == expected
        if route == "calendar":
            calendar = await learning.calendar(user)
            displayed = next(row for row in calendar["events"] if row.id == event.id)
        else:
            displayed = await learning.webinar(UUID(event.id), user)
        assert displayed.admin_link == expected
        assert (displayed.link is not None) == (expected is not None)

    await assert_links(event.admin_link)
    if state == "withdrawn":
        remote[THIRD] = canonical(THIRD)
        await delete_user_data(THIRD)
    else:
        await unavailable(rights[elected_index], state)
        await db.commit()
    await assert_links(None)
    assert len(await db.all(filter_by(WebinarParticipant, webinar_id=event.id))) == 2
    assert {r.payment_id: (required(await db.get(BookingPayment, id=r.payment_id))).user_id for r in rights} == original
    assert (required(await db.get(EventRightGrant, id=grant_id))).state == (
        "withdrawn" if state == "withdrawn" else "granted"
    )


@pytest.mark.parametrize("state", ["review", "closed", "withdrawn", "before_window"])
async def test_scoped_coaching_calendar_uses_retained_host_readiness_and_window(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture, state: str
) -> None:
    from datetime import timedelta

    from api.endpoints import learning
    from api.schemas.user import User, UserInfo
    from api.utils.utc import utcnow
    from tests.services.test_event_succession import authorize
    from tests.services.test_user_deletion import _slot

    info = UserInfo(id=THIRD, name="synthetic", display_name="Synthetic host", avatar_url=None)
    mocker.patch("api.endpoints.calendar.get_userinfo", AsyncMock(return_value=info))
    mocker.patch("api.models.webinars.LecturerRating.get_rating", AsyncMock(return_value=None))
    event = _slot(str(uuid4()), USER, OTHER)
    event.start = utcnow() + timedelta(hours=48 if state == "before_window" else 3)
    event.end = event.start + timedelta(hours=1)
    await db.add(event)
    remote[USER] = canonical(USER)
    await delete_user_data(USER)
    right = required(await retained_events.right_for(required(event.payment_id), "instructor"))
    grant, _, _ = await authorize(mocker, right)
    await retained_events.deliver(USER, grant)
    await db.commit()
    user = User(id=THIRD, email_verified=True, admin=False)
    if state in ("review", "closed"):
        await unavailable(right, state)
        await db.commit()
    elif state == "withdrawn":
        remote[THIRD] = canonical(THIRD)
        await delete_user_data(THIRD)
    view = await learning.existing_events(THIRD, event.id)
    assert (view[0]["link"] if view else None) is None
    displayed = next(row for row in (await learning.calendar(user))["events"] if row.id == event.id)
    assert displayed.admin_link is None and displayed.link is None
    assert event.payment_id == right.payment_id and event.booked_by == OTHER


async def test_mounted_scoped_host_reads_agree_after_exact_contract_review(
    session: AsyncSession, remote: dict[str, Any], mocker: MockerFixture, client: AsyncClient
) -> None:
    from api.app import app
    from api.endpoints import learning
    from api.schemas.user import User, UserInfo

    info = UserInfo(id=THIRD, name="synthetic", display_name="Synthetic host", avatar_url=None)
    for path in ("api.endpoints.calendar.get_userinfo", "api.models.webinars.get_userinfo"):
        mocker.patch(path, AsyncMock(return_value=info))
    mocker.patch("api.models.webinars.LecturerRating.get_rating", AsyncMock(return_value=None))
    event, rights = await two_rights(remote)
    await elect(mocker, rights[0])
    await db.commit()
    app.dependency_overrides[learning.learning_auth] = lambda: User(id=THIRD, email_verified=True, admin=False)
    try:
        paths = ("/learning/events", f"/learning/webinars/{event.id}")
        assert (await client.get("/learning/calendar")).status_code == 410
        for expected in (True, False):
            for path in paths:
                response = await client.get(path)
                assert response.status_code == 200
                value = response.json()
                row = value[0] if path.endswith("/events") else value
                assert (row["link"] is not None) == expected
                if "admin_link" in row:
                    assert (row["admin_link"] is not None) == expected
            if expected:
                await unavailable(rights[0], "review")
                await db.commit()
    finally:
        del app.dependency_overrides[learning.learning_auth]


remote = test_retained_events.remote
