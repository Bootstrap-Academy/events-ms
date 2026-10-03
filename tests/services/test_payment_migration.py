"""Additive upgrade over the actual historical chain; isolated PostgreSQL only."""

import os
import subprocess  # noqa: S404
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest


async def test_actual_chain_preserves_guesses_entitlements_operations_and_guards_downgrade() -> None:
    url = os.getenv("T9_MIGRATION_DB")
    if not url:
        pytest.skip("requires dedicated isolated synthetic migration database")
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg", "postgresql"))
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        env = os.environ | {"DATABASE_URL": url}

        def migrate(*args: str, succeeds: bool = True) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "alembic", *args],
                cwd=Path(__file__).parents[2],
                env=env,
                capture_output=True,
                text=True,
            )
            assert (result.returncode == 0) == succeeds, result.stdout + result.stderr
            return result

        migrate("upgrade", "head")
        migrate("downgrade", "c6e1700ab001")  # empty roundtrip is permitted
        migrate("downgrade", "4c529cdc0409")
        await conn.execute(
            "INSERT INTO events_webinars (id,creator,name,price,start) "
            "VALUES ('old','host','Same name',900,now()+interval '3 days')"
        )
        # Historical data has no paid amount or booking timestamps/ledger keys. A
        # price change, free emergency and cancelled/rebooked user are indistinguishable.
        for user in ["paid-before-price-change", "emergency-free", "discounted", "rebooked"]:
            await conn.execute("INSERT INTO events_webinar_participants (webinar_id,user_id) VALUES ('old',$1)", user)
        await conn.execute(
            "INSERT INTO events_slot (id,user_id,booked_by,event_type,student_coins,instructor_coins,start) "
            "VALUES ('old-coaching','host','student','COACHING',500,350,now())"
        )
        migrate("upgrade", "c6e1700ab001")
        await conn.execute("UPDATE events_webinar_participants SET paid_coins=0 WHERE user_id='emergency-free'")
        batch = str(uuid4())
        operation = str(uuid4())
        completed = str(uuid4())
        await conn.execute(
            "INSERT INTO events_settlement_batches (id,kind,created_at,notifications) "
            "VALUES ($1,'cancellation',now(),'[]')",
            batch,
        )
        for op, done in [(operation, False), (completed, True)]:
            await conn.execute(
                "INSERT INTO events_coin_operations (id,batch_id,event_id,user_id,coins,description, "
                "credit_note,attempts,completed_at) "
                "VALUES ($1,$2,'deleted-event','student',999,'original immutable payload',0,2, "
                "CASE WHEN $3 THEN now() ELSE NULL END)",
                op,
                batch,
                done,
            )
        migrate("upgrade", "head")
        rows = await conn.fetch(
            "SELECT p.user_id,p.paid_coins,b.original,b.state FROM events_webinar_participants p "
            "JOIN events_booking_payments b ON p.payment_id=b.id ORDER BY p.user_id"
        )
        assert len(rows) == 4 and all(r["paid_coins"] is None and r["state"] == "legacy_unknown" for r in rows)
        import json

        originals = {r["user_id"]: json.loads(r["original"]) for r in rows}
        assert originals["emergency-free"]["paid_coins"] == 0
        assert originals["paid-before-price-change"]["paid_coins"] == 900
        slot = await conn.fetchrow(
            "SELECT booked_by,student_coins,instructor_coins,payment_id FROM events_slot WHERE id='old-coaching'"
        )
        assert (
            slot["booked_by"] == "student"
            and slot["payment_id"]
            and slot["student_coins"] is None
            and slot["instructor_coins"] is None
        )
        assert await conn.fetchval("SELECT count(*) FROM events_booking_payments") == 5
        assert (
            await conn.fetchval("SELECT provenance FROM events_coin_operations WHERE id=$1", operation) == "legacy_hold"
        )
        assert (
            await conn.fetchval("SELECT provenance FROM events_coin_operations WHERE id=$1", completed)
            == "legacy_completed"
        )
        assert await conn.fetchval("SELECT coins FROM events_coin_operations WHERE id=$1", operation) == 999
        result = migrate("downgrade", "c6e1700ab001", succeeds=False)
        assert "Cannot remove booking payment/claim evidence" in result.stderr
        assert await conn.fetchval("SELECT count(*) FROM events_booking_payments") == 5
        assert await conn.fetchval("SELECT count(*) FROM events_webinar_participants") == 4
    finally:
        await conn.close()
