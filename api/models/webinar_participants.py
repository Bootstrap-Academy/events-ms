from typing import Any

from sqlalchemy import BigInteger, Column, ForeignKey, String, event, inspect
from sqlalchemy.orm import Mapped, Session, relationship

from api.database import Base
from api.models.webinars import Webinar


class WebinarParticipant(Base):
    __tablename__ = "events_webinar_participants"

    webinar_id: Mapped[str] = Column(String(36), ForeignKey("events_webinars.id"), primary_key=True)
    webinar: Webinar = relationship("Webinar", back_populates="participants", lazy="selectin")
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    # NULL means unknown, never free. The durable journal retains each registration identity.
    paid_coins: Mapped[int | None] = Column(BigInteger, nullable=True)
    payment_id: Mapped[str | None] = Column(String(36), nullable=True, unique=True)


# A seat PK can move or disappear. An old snapshot/identity-map entry is not
# evidence that it still exists. Record only actual ORM flush changes so fresh
# committed occupancy can be overlaid with this transaction's own writes.
_SEAT_CHANGES = "events_seat_changes"
_SEAT_SAVEPOINTS = "events_seat_savepoints"


@event.listens_for(Session, "after_flush")
def _record_seat_changes(session: Session, _: Any) -> None:
    changed = [
        row
        for row in session.new | session.dirty | session.deleted
        if isinstance(row, WebinarParticipant)
        and (row in session.new or row in session.deleted or session.is_modified(row, include_collections=False))
    ]
    if not changed:
        return
    changes = session.info.setdefault(_SEAT_CHANGES, {})
    for row in changed:
        previous = inspect(row).identity
        if previous is not None:
            changes[previous] = None
    for row in changed:
        if row not in session.deleted:
            changes[(row.webinar_id, row.user_id)] = (row.payment_id,)


@event.listens_for(Session, "after_transaction_create")
def _save_seat_changes(session: Session, transaction: Any) -> None:
    if transaction.nested:
        session.info.setdefault(_SEAT_SAVEPOINTS, {})[transaction] = dict(session.info.get(_SEAT_CHANGES, {}))


@event.listens_for(Session, "after_soft_rollback")
def _restore_seat_changes(session: Session, transaction: Any) -> None:
    if transaction.nested:
        session.info[_SEAT_CHANGES] = session.info.get(_SEAT_SAVEPOINTS, {}).pop(transaction, {})


@event.listens_for(Session, "after_transaction_end")
def _clear_seat_changes(session: Session, transaction: Any) -> None:
    if transaction.parent is None:
        session.info.pop(_SEAT_CHANGES, None)
        session.info.pop(_SEAT_SAVEPOINTS, None)


def own_seat_changes(session: Session) -> dict[tuple[str, str], tuple[str | None] | None]:
    """Flushed changes only; root commit/rollback and savepoint rollback are scoped."""
    return dict(session.info.get(_SEAT_CHANGES, {}))
