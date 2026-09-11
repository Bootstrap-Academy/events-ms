"""Deletion fairness and retained claims over real PostgreSQL and backend coin HTTP."""

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from api.database import db, db_context, select
from api.models import BookingPayment, CoinOperation, EmergencyCancel, SettlementBatch
from api.services.user_deletion import delete_user_data
from api.sweep import sweep_deleted_users
from tests.services.test_settlements import BAR, FOO, HOST, seed


pytest_plugins = ["tests.services.test_settlements"]


async def test_deleted_recipient_claim_survives_replay_without_starving_next_user(
    ledger: Any, mocker: MockerFixture
) -> None:
    await seed()
    # The original recipient is gone; a new credit cannot be paid to a substitute.
    await ledger.execute("DELETE FROM users WHERE id=$1", UUID(FOO))
    later = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    async with db_context():
        await db.add(EmergencyCancel(user_id=later))
    mocker.patch("api.sweep.exists_user_uncached", AsyncMock(side_effect=lambda uid: uid == BAR))
    await sweep_deleted_users()
    async with db_context():
        assert await db.all(select(EmergencyCancel)) == []
        operations = await db.all(select(CoinOperation))
        pending = [op for op in operations if op.user_id == FOO]
        assert len(pending) == 1 and pending[0].completed_at is None
        original_id = pending[0].id
        assert pending[0].coins == 100 and pending[0].attempts >= 1
        assert len([op for op in operations if op.user_id == BAR and op.completed_at is not None]) == 1
        batches = await db.count(select(SettlementBatch))
    with pytest.raises(HTTPException):
        async with db_context():
            await delete_user_data(HOST)
    async with db_context():
        assert await db.count(select(SettlementBatch)) == batches
        operation = await db.get(CoinOperation, id=original_id)
        assert operation is not None and operation.completed_at is None
        assert len(await db.all(select(BookingPayment))) == 2
    assert await ledger.fetchval("SELECT count(*) FROM internal_coin_operations WHERE id=$1", UUID(original_id)) == 0


async def test_known_student_booking_keeps_dated_deletion_evidence(ledger: Any) -> None:
    await seed()
    async with db_context():
        payment = await db.first(select(BookingPayment).where(BookingPayment.user_id == BAR))
        assert payment is not None
        original_id = payment.id
        await delete_user_data(BAR)
    async with db_context():
        payment = await db.get(BookingPayment, id=original_id)
        assert payment is not None
        assert payment.user_id == BAR and payment.paid_coins == 100
        assert payment.original["student_deletion_claim"] is True
        assert payment.original["deletion_requested_at"]
        assert await db.all(select(CoinOperation)) == []  # Entitlement policy remains queued.
