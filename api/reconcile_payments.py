"""Local operator evidence review. Reporting is read-only; resolution never sends coins.

Run `python -m api.reconcile_payments report` or `resolve manifest.json`.
The backend connection is a read-only ledger lookup, never a name/price matcher.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from api.database import db, db_context, filter_by, select
from api.models import BookingPayment, CoinOperation, SettlementClaim, Slot, Webinar, WebinarParticipant
from api.utils.utc import utcnow


def document(path: str, expected_hash: str) -> dict[str, str]:
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if not data.strip() or digest != expected_hash:
        raise ValueError("Reviewed supporting document is empty or its digest changed")
    return {"sha256": digest, "path": path}


async def report() -> dict[str, Any]:
    payments = await db.all(select(BookingPayment))
    claims = await db.all(select(SettlementClaim))
    operations = await db.all(select(CoinOperation))
    return {
        "generated_at": utcnow().isoformat(),
        "bookings": [
            {
                "id": p.id,
                "event_id": p.event_id,
                "user_id": p.user_id,
                "kind": p.kind,
                "state": p.state,
                "paid_coins": p.paid_coins,
                "quoted_coins": p.quoted_coins,
                "original": p.original,
                "evidence": p.evidence,
                "last_error": p.last_error,
            }
            for p in payments
        ],
        "unresolved_claims": [
            {
                "id": c.id,
                "event_id": c.event_id,
                "user_id": c.user_id,
                "payment_ids": c.payment_ids,
                "created_at": c.created_at.isoformat(),
                "ratio": c.ratio,
                "amount_field": c.amount_field,
            }
            for c in claims
            if c.coins is None
        ],
        "legacy_operations": [
            {
                "id": o.id,
                "event_id": o.event_id,
                "user_id": o.user_id,
                "coins": o.coins,
                "provenance": o.provenance,
                "completed_at": str(o.completed_at),
            }
            for o in operations
            if o.provenance != "ready"
        ],
        "pending_booking_count": sum(p.state == "pending" for p in payments),
        "unknown_booking_count": sum(p.state == "legacy_unknown" for p in payments),
        "student_deletion_claim_count": sum(bool(p.original.get("student_deletion_claim")) for p in payments),
    }


async def resolve(manifest: dict[str, Any], ledger: Any) -> None:
    # A reviewer must corroborate the particular registration, not merely a user,
    # matching name, nearby timestamp, current price, or absence of a transaction.
    if (
        manifest.get("booking_link_reviewed") is not True
        or not manifest.get("reviewed_by")
        or not manifest.get("basis")
    ):
        raise ValueError("Explicit historical booking-link review and basis are required")
    proof = document(manifest["document_path"], manifest["document_sha256"])
    payment = await db.get(BookingPayment, id=manifest["payment_id"])
    if payment is None:
        raise ValueError("Unknown payment identity")
    model = Webinar if payment.kind == "webinar" else Slot
    await db.first(filter_by(model, id=payment.event_id).with_for_update())
    payment = await db.first(
        filter_by(BookingPayment, id=payment.id).with_for_update().execution_options(populate_existing=True)
    )
    if payment is None or payment.state != "legacy_unknown":
        raise ValueError("Only unresolved legacy bookings may be resolved; evidence cannot be overwritten")
    if manifest["event_id"] != payment.event_id or manifest["user_id"] != payment.user_id:
        raise ValueError("Manifest does not identify this registration")
    evidence: dict[str, Any] = {
        "reviewed_by": manifest["reviewed_by"],
        "basis": manifest["basis"],
        "document": proof,
        "reviewed_at": utcnow().isoformat(),
        "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
    }
    if manifest["kind"] == "documented_free_booking":
        if manifest.get("transaction_id") is not None:
            raise ValueError("Free booking cannot adopt a debit")
        paid = 0
        evidence["kind"] = "reviewed_no_charge"
    elif manifest["kind"] == "documented_ledger_debit":
        if ledger is None:
            raise ValueError("A read-only backend ledger connection is required")
        async with ledger.transaction(readonly=True):
            row = await ledger.fetchrow(
                "SELECT id, user_id, coins, description, created_at, include_in_credit_note "
                "FROM transactions WHERE id=$1",
                UUID(manifest["transaction_id"]),
            )
        if row is None or str(row["user_id"]) != payment.user_id or row["coins"] >= 0 or row["include_in_credit_note"]:
            raise ValueError("Ledger evidence is not a debit for this participant")
        if row["created_at"] > payment.created_at:
            raise ValueError("Ledger entry postdates this legacy evidence snapshot")
        paid = -row["coins"]
        payment.ledger_transaction_id = str(row["id"])  # unique: no debit can fund two rebookings
        evidence |= {
            "kind": "reviewed_ledger_debit",
            "transaction": {key: str(value) for key, value in dict(row).items()},
        }
    else:
        raise ValueError("Unsupported evidence kind")
    payout = 0 if paid == 0 else None
    if payment.kind == "coaching" and paid > 0:
        payout = manifest.get("payout_coins")
        if not isinstance(payout, int) or isinstance(payout, bool) or not 0 <= payout <= paid:
            raise ValueError("Historical coaching payout requires a reviewed nonnegative amount within the paid charge")
        evidence["payout_terms"] = document(manifest["payout_document_path"], manifest["payout_document_sha256"])
    payment.paid_coins = paid
    payment.payout_coins = payout
    payment.state = "resolved"
    payment.evidence = evidence
    if payment.kind == "webinar":
        participant = await db.get(WebinarParticipant, payment_id=payment.id)
        if participant:
            participant.paid_coins = paid
    else:
        slot = await db.get(Slot, payment_id=payment.id)
        if slot:
            slot.student_coins = paid
            slot.instructor_coins = payout
    # No credit or notification is sent here. The existing durable claim worker
    # later creates the original claim UUID's T6 operation at the saved fraction.
    await db.commit()


async def run(args: argparse.Namespace) -> None:
    async with db_context():
        if args.command == "report":
            print(json.dumps(await report(), indent=2))
        else:
            ledger = await asyncpg.connect(args.backend_database_url) if args.backend_database_url else None
            try:
                await resolve(json.loads(Path(args.manifest).read_text()), ledger)
            finally:
                if ledger:
                    await ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("report")
    resolver = commands.add_parser("resolve")
    resolver.add_argument("manifest")
    resolver.add_argument("--backend-database-url", help="Use a read-only backend credential; never printed")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
