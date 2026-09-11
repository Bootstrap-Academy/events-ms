# Booking payment provenance and reconciliation

Revision `d9b001pay001` must run with all old Events writers stopped. It is an additive successor to `c6e1700ab001`. Do not start an intermediate runtime after the older `bbe2621aea9c` price backfill. The older migration remains unchanged because it may already have run. The new migration preserves every old webinar participant and booked coaching's asserted amounts and context in `events_booking_payments.original`, gives each registration a durable identity, and sets unproven charge/payout fields to NULL. This includes old zero amounts: neither the configured price nor an old emergency flag establishes what that registration paid. No production population size is assumed.

`paid_coins = NULL` means unknown or awaiting acknowledgement. It never means free. `quoted_coins` is only the immutable new booking quote, not proof of a debit. An acknowledged keyed debit establishes the paid amount; a transactionally recorded zero price/emergency waiver establishes a free booking. An exact insufficient-funds rejection establishes a failed booking with no charge. Unknown backend/transport outcomes remain pending, keeping the reservation and its identity. Retry/recovery uses the same key and quote. Price edits, cancellation and rebooking cannot replace that identity or resurrect an earlier attendance record. Reservations continue to occupy capacity; their payment status is visible. The same backend key works for debit and credit, but their identities are distinct.

Bookings, reservations and waiver consumption commit before any remote debit. Successful acknowledgement and the charge update commit before cache/notification work. Startup and the five-minute loop recover pending booking debits before settlement cleanup. Neither old unkeyed POST nor a current-price fallback is used. A backend without keyed-operation support leaves bookings pending. A definitively rejected debit removes only its own active reservation; its journal remains. Outages can reserve capacity pending review; operators must monitor this backlog.

Cancellation stores dated `events_settlement_claims`, including the original registration IDs, recipient, refund/payout fraction and credit-note flag, in the same transaction that removes attendance. Unknown amounts remain unpaid claims. HTTP 503 `EventSettlementPending` explicitly reports `cancellation_committed: true`; repeating the receipt does not create a new obligation. Once all required amounts are proven, recovery creates the keyed coin operation with the **claim's original UUID**. No guessed or zero coin credit is emitted for an unresolved claim. Payment notices wait until the amounts are known and credits acknowledged. The backend transaction/idempotency guards remain mandatory.

Deletion of an instructor's future events uses the same claims. An uncertain booking belonging to a deleting student retains `student_deletion_claim` and the request time in its journal. This is a manual claim flag, not a forfeiture or a new refund formula. Resolve the deleting student's claim and verify a destination for a recipient who no longer has an account before any settlement. The deletion marker does not define a new refund policy. The journal and claims have no event/user cascade.

## Operator report and evidence adoption

Run `python -m api.reconcile_payments report` in the selected Events runtime/configuration. This performs no coin delivery or history matching. Its output contains user IDs and financial evidence: retain it in the restricted financial case archive, not a public log. Monitor pending/unknown booking counts, unresolved claims, student deletion flags, and all legacy operations. The recovery worker also logs pending/unknown counts and pending credit/claim totals.

Historical backend `transactions` contain a user, signed amount, timestamp, free-text description and credit-note flag, but no booking UUID. Webinar names could change or repeat; coaching debits say only `Coaching`; cancellation/rebooking can create multiple debits; emergency/free bookings may leave no debit. A name/time/current-price match, missing ledger row, account balance or current emergency flag therefore cannot automatically prove a historical charge or zero price. Review the original booking/receipt/waiver and refund/rebooking history together. If that cannot uniquely establish the registration, leave it unknown and preserve its claim. There is no automatic zero-user assumption or bulk price backfill.

A reviewer may adopt an independently corroborated historical debit using a local JSON manifest:

```json
{
  "payment_id": "UUID from the Events report",
  "event_id": "original event ID",
  "user_id": "participant UUID",
  "kind": "documented_ledger_debit",
  "transaction_id": "exact backend transaction UUID",
  "booking_link_reviewed": true,
  "reviewed_by": "responsible reviewer/case identity",
  "basis": "How original documents distinguish this registration from free, discounted, cancelled or rebooked alternatives",
  "document_path": "/restricted/archive/case-evidence.pdf",
  "document_sha256": "SHA-256 of retained corroborating evidence"
}
```

Run `python -m api.reconcile_payments resolve manifest.json --backend-database-url <read-only-backend-connection>`. Use a restricted local configuration and read-only backend credential; avoid putting real secrets in shell history. The CLI checks the document digest, explicit review, event/user identity, exact ledger entry, debit direction, owner, credit-note exclusion, and that the debit predates the legacy snapshot. It takes the **actual ledger amount**, never an operator-supplied/current price. A unique transaction ID prevents reuse for multiple registrations. Adoption is one-way; an already resolved record cannot be overwritten. The reviewer must establish booking correlation: software cannot authenticate the truth of an arbitrary supporting document or replace the human review. Keep the referenced document and reviewed manifest durably with the financial evidence; only their identity/digest, review basis and the exact ledger row are copied into the journal.

For a documented free registration, use `kind: "documented_free_booking"` and omit `transaction_id`; retain affirmative contemporaneous no-charge/waiver evidence. Absence of a debit is not such evidence. A paid legacy coaching also requires `payout_coins`, `payout_document_path` and `payout_document_sha256` establishing its agreed instructor share; the old asserted payout is not adopted automatically. Amounts must be nonnegative and no greater than the proven charge. Final financial/contract review remains necessary if the share itself is disputed. Resolution sends no coins or mail; the separate claim worker later delivers the preserved obligation.

## Existing credits, release and recovery gates

The migration marks all preexisting unacknowledged keyed coin operations `legacy_hold` and acknowledged ones `legacy_completed`, preserving UUID, amount, description, attempts, timestamps and any earlier outcome. They may already contain a guessed historical amount; no automatic retry, rewrite, reversal or replacement credit is authorized by this migration. The report includes both groups. A missing local acknowledgement may still mean the backend credited the same key. Reconcile the backend's immutable operation and ledger evidence first. Correct held requests and incorrectly paid requests require a separately reviewed case-specific forward repair; keep them held until that repair establishes the original outcome and any remaining claim without duplicate credit. Do not manually change a sent operation's payload or delete its evidence to force a retry. Historical unkeyed partial effects also require review and cannot be deduplicated retroactively.

Before release: inventory actual legacy bookings and old operations, retain snapshots and case evidence, assign reconciliation ownership/alerts and retained-claim access, establish the compatible backend keyed-operation schema and binary, stop/drain every old Events process/sweep/manual writer, and update the final cross-repository manifest/pins in the infrastructure release procedure. Regenerate the release manifest whenever selected sources change. Preserve the complete Events and backend financial evidence on recovery. Downgrade refuses removal when booking/claim/operation evidence exists. An older binary does not understand nullable charges, pending reservations, or held operations; keep writers closed and use compatible forward repair. No production rollout, historical reconciliation, document retention deployment or policy approval is established merely by running this migration or its synthetic tests.
