"""Scheduling tools (D10, D3 addendum). Every account tool takes the verified session, never a
customer ID (rule 6). Writes are conditional on current status and checked by row count, in one
transaction (rule 5), so a slot can't be double-booked. No tool changes money (rule 7): a late
change only records the caller's fee consent for staff review.
"""

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import cache
from typing import Literal

from meridian import clock
from meridian.identity import IdentitySession, current_customer
from meridian.kb.manifest import load as load_manifest
from meridian.scheduling.zones import zone_for

SERVICE_TYPES = ("hvac_repair", "hvac_tune_up", "plumbing", "estimate")
MEMBER_TIERS = ("standard", "plus")
EVENING = "16:00-19:00"  # Tue/Thu only (SCHEMA.md)
OFFER_SIZE = 3
WARRANTY_YEARS = 2  # D10.10: install labor warranty, applied to everyone
FEE_DOC = "pricing/2026_fee_schedule.pdf"
_FEE_ROW = re.compile(r"^\|\s*Late cancellation \(<24h\)\s*\|\s*\$(\d+)(?:\.(\d\d))?\s*\|", re.M)


@cache
def late_change_fee_cents() -> int:
    """The late-change fee, read from Meridian's current fee schedule (rule 8: no KB facts in
    code). Called at app startup, so a fee schedule without the row stops the app loudly."""
    doc = next((d for d in load_manifest() if d.path == FEE_DOC and d.status == "current"), None)
    if doc is None:
        raise RuntimeError(f"{FEE_DOC} is not a current document in the KB manifest")
    m = _FEE_ROW.search(doc.text_path.read_text())
    if m is None:
        raise RuntimeError(f"no late-cancellation fee found in {FEE_DOC}")
    return int(m.group(1)) * 100 + int(m.group(2) or 0)


def fee_button() -> str:
    return f"I agree to the ${late_change_fee_cents() / 100:g} fee"


def fee_notice() -> str:
    return (
        f"Because this is within 24 hours, Meridian's ${late_change_fee_cents() / 100:g} "
        "late-change fee applies. A team member will review it and confirm with you before "
        f'anything is charged. To go ahead, tap "{fee_button()}" or type "I agree".'
    )


class Handoff(StrEnum):
    """Reasons the scheduling tools can't finish the request; the agent hands off (D10, D11)."""

    NO_ZONE = "address_outside_scheduler_zones"  # e.g. Manor, Dripping Springs (I14)
    NO_SLOTS_FOR_SERVICE = "no_slots_for_service_in_zone"  # e.g. north plumbing (I15)
    NONE_AVAILABLE = "no_open_slots"
    SAME_DAY = "same_day_request"
    FEE_DECLINED = "late_change_fee_declined"  # D11 "couldn't do"
    URGENT = "urgent"  # set by the agent (D10.11); listed here so handoffs share one vocabulary


class NotOffered(Exception):
    """Code guard (D10.6): only a slot offered in this conversation can be booked."""


class NotFound(Exception):
    """No such upcoming appointment for this caller (same error whether it exists or not)."""


@dataclass(frozen=True)
class Slot:
    slot_id: int
    date: date
    window: str
    service_type: str
    tech_name: str


@dataclass(frozen=True)
class Appointment:
    appointment_id: int
    slot: Slot
    issue: str | None
    is_warranty_visit: bool


@dataclass(frozen=True)
class SlotTaken:
    """The slot was booked by someone else between offer and booking; search again."""


@dataclass(frozen=True)
class NeedsFeeConsent:
    amount_cents: int
    message: str


@dataclass(frozen=True)
class FeeConsent:
    """Built by the chat layer from the button press or the caller's literal next message —
    never from model tool arguments (D10.8, D20.1)."""

    text: str  # exactly what the caller sent, or the button label
    method: Literal["button", "typed"]


@dataclass(frozen=True)
class PendingFee:
    """The change the caller was asked to consent to; consent applies to this and nothing else."""

    change: Literal["reschedule", "cancel"]
    appointment_id: int
    slot_id: int | None
    notice: str


@dataclass(frozen=True)
class Done:
    appointment: Appointment
    fee_review_id: int | None = None


@dataclass
class SchedulingState:
    """Per-conversation: the slots offered most recently (D10.6)."""

    offered: list[int] = field(default_factory=list[int])
    pending_fee: PendingFee | None = None


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9$ ]", " ", s.lower()).split())


def is_fee_consent(c: FeeConsent) -> bool:
    """Formal consent (D10.8): the button, or typed "I agree" (case and punctuation forgiven)."""
    if c.method == "button":
        return c.text == fee_button()
    return _norm(c.text) in ("i agree", _norm(fee_button()))


def _window_start(d: date, window: str) -> datetime:
    return datetime.combine(d, time(int(window[:2])))


def _bookable_statuses(tier: str) -> tuple[str, str]:
    """Always two values so the SQL is a fixed `status IN (?, ?)` (rule 4)."""
    return ("open", "reserved_members") if tier in MEMBER_TIERS else ("open", "open")


_SLOT_SQL = (
    "SELECT s.slot_id, s.slot_date, s.window, s.service_type, t.name AS tech_name "
    "FROM appointment_slots s JOIN technicians t ON t.tech_id = s.tech_id "
)


def _slot(r: sqlite3.Row) -> Slot:
    return Slot(
        int(r["slot_id"]),
        date.fromisoformat(r["slot_date"]),
        str(r["window"]),
        str(r["service_type"]),
        str(r["tech_name"]),
    )


def _evening_ok(s: Slot) -> bool:
    return s.window != EVENING or s.date.weekday() in (1, 3)


def find_slots(
    conn: sqlite3.Connection,
    session: IdentitySession,
    state: SchedulingState,
    service_type: str,
    on: date | None = None,
    window: str | None = None,
    earlier_than: tuple[date, str] | None = None,
) -> list[Slot] | Handoff:
    """Up to 3 bookable slots in the caller's zone, nearest to the request (D10.4–5).

    `on`/`window`: what the caller asked for (exact match first, then nearest days).
    `earlier_than`: "anything earlier?" — only slots before that date and window, earliest first.
    """
    if service_type not in SERVICE_TYPES:
        raise ValueError(f"unknown service type {service_type!r}")
    cust = current_customer(conn, session)
    zone = zone_for(str(cust["city"]))
    if zone is None:
        return Handoff.NO_ZONE
    today = clock.today()
    if on is not None and on <= today:
        return Handoff.SAME_DAY
    if not conn.execute(
        "SELECT 1 FROM appointment_slots WHERE zone = ? AND service_type = ? LIMIT 1",
        (zone, service_type),
    ).fetchone():
        return Handoff.NO_SLOTS_FOR_SERVICE
    statuses = _bookable_statuses(str(cust["membership_tier"]))
    rows = conn.execute(
        _SLOT_SQL + "WHERE s.zone = ? AND s.service_type = ? AND s.slot_date > ? "
        "AND s.status IN (?, ?)",
        (zone, service_type, today.isoformat(), *statuses),
    ).fetchall()
    slots = [s for s in map(_slot, rows) if _evening_ok(s)]
    if earlier_than is not None:
        slots = [s for s in slots if (s.date, s.window) < earlier_than]

    def key(s: Slot) -> tuple[int, bool, date, str]:
        if on is None or earlier_than is not None:
            return (0, False, s.date, s.window)
        return (
            abs((s.date - on).days),
            window is not None and s.window != window,
            s.date,
            s.window,
        )

    picked = sorted(slots, key=key)[:OFFER_SIZE]
    if not picked and earlier_than is None:
        return (
            Handoff.NONE_AVAILABLE
        )  # an empty list means "nothing earlier": answer, don't hand off
    state.offered = [s.slot_id for s in picked]
    return picked


def _warranty(install: str | None) -> bool:
    if not install:
        return False
    t = clock.today()
    try:
        cutoff = t.replace(year=t.year - WARRANTY_YEARS)
    except ValueError:  # Feb 29
        cutoff = t.replace(year=t.year - WARRANTY_YEARS, day=28)
    return date.fromisoformat(install) > cutoff


def _atomic[T](conn: sqlite3.Connection, work: Callable[[], T]) -> T:
    """One write transaction, taken up front so concurrent bookings queue instead of racing."""
    if conn.in_transaction:
        raise RuntimeError("scheduling writes must not run inside another transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = work()
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return out


class _Taken(Exception):
    pass


def _claim(conn: sqlite3.Connection, slot_id: int, zone: str, service: str, tier: str) -> None:
    statuses = _bookable_statuses(tier)
    cur = conn.execute(
        "UPDATE appointment_slots SET status = 'booked' WHERE slot_id = ? AND zone = ? "
        "AND service_type = ? AND slot_date > ? AND status IN (?, ?)",
        (slot_id, zone, service, clock.today().isoformat(), *statuses),
    )
    if cur.rowcount != 1:
        raise _Taken


def _get_slot(conn: sqlite3.Connection, slot_id: int) -> Slot:
    return _slot(conn.execute(_SLOT_SQL + "WHERE s.slot_id = ?", (slot_id,)).fetchone())


def book(
    conn: sqlite3.Connection,
    session: IdentitySession,
    state: SchedulingState,
    slot_id: int,
    issue: str,
) -> Done | SlotTaken:
    """Book an offered slot for the verified caller (D10.6–7, 10, 12)."""
    cust = current_customer(conn, session)
    if slot_id not in state.offered:
        raise NotOffered
    zone = zone_for(str(cust["city"]))
    if zone is None:
        raise NotOffered
    slot = _get_slot(conn, slot_id)
    warranty = _warranty(cust["system_install_date"])

    def work() -> int:
        _claim(conn, slot_id, zone, slot.service_type, str(cust["membership_tier"]))
        cur = conn.execute(
            "INSERT INTO appointments (slot_id, customer_id, issue_description, created_at, "
            "status, is_warranty_visit) VALUES (?, ?, ?, ?, 'scheduled', ?)",
            (slot_id, cust["customer_id"], issue[:200], clock.today().isoformat(), int(warranty)),
        )
        return int(cur.lastrowid or 0)

    try:
        appt_id = _atomic(conn, work)
    except _Taken:
        state.offered = [s for s in state.offered if s != slot_id]
        return SlotTaken()
    state.offered = []
    return Done(Appointment(appt_id, slot, issue[:200], warranty))


def get_appointments(conn: sqlite3.Connection, session: IdentitySession) -> list[Appointment]:
    """The verified caller's upcoming scheduled visits (confirm, D10.2)."""
    cid = session.require_customer()
    rows = conn.execute(
        "SELECT a.appointment_id, a.issue_description, a.is_warranty_visit, s.slot_id, "
        "s.slot_date, s.window, s.service_type, t.name AS tech_name FROM appointments a "
        "JOIN appointment_slots s ON s.slot_id = a.slot_id "
        "JOIN technicians t ON t.tech_id = s.tech_id "
        "WHERE a.customer_id = ? AND a.status = 'scheduled' AND s.slot_date >= ? "
        "ORDER BY s.slot_date, s.window",
        (cid, clock.today().isoformat()),
    ).fetchall()
    return [
        Appointment(
            int(r["appointment_id"]), _slot(r), r["issue_description"], bool(r["is_warranty_visit"])
        )
        for r in rows
    ]


class SlotStateMismatch(Exception):
    """The visit's slot wasn't 'booked' as expected; data needs a person, not a retry."""


def _own_appointment(
    conn: sqlite3.Connection, session: IdentitySession, appointment_id: int
) -> Appointment:
    for a in get_appointments(conn, session):
        if a.appointment_id == appointment_id:
            return a
    raise NotFound


def _changeable(a: Appointment) -> bool:
    """Same-day (or already started) visits can't be changed here; they hand off (D10.1)."""
    return a.slot.date > clock.today()


def _late(a: Appointment) -> bool:
    return _window_start(a.slot.date, a.slot.window) - clock.now() < timedelta(hours=24)


def _fee_review(
    conn: sqlite3.Connection,
    conversation_id: str,
    appointment_id: int,
    change: str,
    consent: FeeConsent,
    notice: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO fee_reviews (conversation_id, appointment_id, change, amount_cents, "
        "consent_text, consent_method, consent_at, status, notice_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (
            conversation_id,
            appointment_id,
            change,
            late_change_fee_cents(),
            consent.text[:200],
            consent.method,
            clock.now().isoformat(),
            notice,
        ),
    )
    return int(cur.lastrowid or 0)


def _consent_ok(state: SchedulingState, pending: PendingFee, consent: FeeConsent | None) -> bool:
    """Consent counts only for the exact change it was asked for, and only once."""
    return consent is not None and is_fee_consent(consent) and state.pending_fee == pending


def _ask_consent(state: SchedulingState, pending: PendingFee) -> NeedsFeeConsent:
    state.pending_fee = pending
    return NeedsFeeConsent(late_change_fee_cents(), pending.notice)


def reschedule(
    conn: sqlite3.Connection,
    session: IdentitySession,
    state: SchedulingState,
    conversation_id: str,
    appointment_id: int,
    slot_id: int,
    consent: FeeConsent | None = None,
) -> Done | SlotTaken | NeedsFeeConsent | Handoff:
    """Move the caller's visit to an offered slot of the same service; free the old slot."""
    cust = current_customer(conn, session)
    old = _own_appointment(conn, session, appointment_id)
    if not _changeable(old):
        return Handoff.SAME_DAY
    if slot_id not in state.offered:
        raise NotOffered
    new = _get_slot(conn, slot_id)
    zone = zone_for(str(cust["city"]))
    if zone is None or new.service_type != old.slot.service_type:
        raise NotOffered
    late = _late(old)
    pending = PendingFee("reschedule", appointment_id, slot_id, fee_notice())
    if late and not _consent_ok(state, pending, consent):
        return _ask_consent(state, pending)

    def work() -> int | None:
        _claim(conn, slot_id, zone, new.service_type, str(cust["membership_tier"]))
        moved = conn.execute(
            "UPDATE appointments SET slot_id = ? WHERE appointment_id = ? AND slot_id = ? "
            "AND customer_id = ? AND status = 'scheduled'",
            (slot_id, appointment_id, old.slot.slot_id, cust["customer_id"]),
        ).rowcount
        freed = conn.execute(
            "UPDATE appointment_slots SET status = 'open' WHERE slot_id = ? AND status = 'booked'",
            (old.slot.slot_id,),
        ).rowcount
        if moved != 1:
            raise _Taken
        if freed != 1:
            raise SlotStateMismatch
        if late and consent is not None:
            return _fee_review(
                conn, conversation_id, appointment_id, "reschedule", consent, pending.notice
            )
        return None

    try:
        fee_id = _atomic(conn, work)
    except _Taken:
        state.offered = [s for s in state.offered if s != slot_id]
        return SlotTaken()
    state.offered, state.pending_fee = [], None
    return Done(Appointment(appointment_id, new, old.issue, old.is_warranty_visit), fee_id)


def cancel(
    conn: sqlite3.Connection,
    session: IdentitySession,
    state: SchedulingState,
    conversation_id: str,
    appointment_id: int,
    consent: FeeConsent | None = None,
) -> Done | NeedsFeeConsent | Handoff:
    """Cancel the caller's visit and free the slot."""
    cid = session.require_customer()
    a = _own_appointment(conn, session, appointment_id)
    if not _changeable(a):
        return Handoff.SAME_DAY
    late = _late(a)
    pending = PendingFee("cancel", appointment_id, None, fee_notice())
    if late and not _consent_ok(state, pending, consent):
        return _ask_consent(state, pending)

    def work() -> int | None:
        n = conn.execute(
            "UPDATE appointments SET status = 'cancelled' WHERE appointment_id = ? "
            "AND customer_id = ? AND status = 'scheduled'",
            (appointment_id, cid),
        ).rowcount
        freed = conn.execute(
            "UPDATE appointment_slots SET status = 'open' WHERE slot_id = ? AND status = 'booked'",
            (a.slot.slot_id,),
        ).rowcount
        if n != 1:
            raise NotFound
        if freed != 1:
            raise SlotStateMismatch
        if late and consent is not None:
            return _fee_review(
                conn, conversation_id, appointment_id, "cancel", consent, pending.notice
            )
        return None

    fee_id = _atomic(conn, work)
    state.pending_fee = None
    return Done(a, fee_id)


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    issued: date
    due: date
    amount_cents: int
    balance_cents: int
    line_items: str  # JSON as stored; the agent reads descriptions and cents


def get_invoices(conn: sqlite3.Connection, session: IdentitySession) -> list[Invoice]:
    """Read-only invoice lookup for the verified caller (D3 addendum)."""
    rows = conn.execute(
        "SELECT invoice_id, issued_date, due_date, amount_cents, balance_cents, line_items "
        "FROM invoices WHERE customer_id = ? ORDER BY issued_date DESC",
        (session.require_customer(),),
    ).fetchall()
    return [
        Invoice(
            str(r["invoice_id"]),
            date.fromisoformat(r["issued_date"]),
            date.fromisoformat(r["due_date"]),
            int(r["amount_cents"]),
            int(r["balance_cents"]),
            str(r["line_items"]),
        )
        for r in rows
    ]


def get_membership(conn: sqlite3.Connection, session: IdentitySession) -> tuple[str, date | None]:
    """(tier, renewal date) for the verified caller (D3 addendum)."""
    c = current_customer(conn, session)
    renewal = c["membership_renewal_date"]
    return str(c["membership_tier"]), date.fromisoformat(renewal) if renewal else None
