"""T8 scheduling tools (D10, D3 addendum) on temp DB copies."""

import sqlite3
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from meridian import clock
from meridian.db import connect, init_db
from meridian.identity import IdentitySession, NotVerified, verify
from meridian.scheduling import (
    Done,
    FeeConsent,
    Handoff,
    NeedsFeeConsent,
    NotFound,
    NotOffered,
    SchedulingState,
    Slot,
    SlotTaken,
    book,
    cancel,
    fee_button,
    fee_notice,
    find_slots,
    get_appointments,
    get_invoices,
    get_membership,
    late_change_fee_cents,
    reschedule,
)

LINDA = ("Linda Foster", "512-555-0101")  # MHC-10001, Round Rock (north), standard
OMAR = ("Omar Patel", "512-555-0102")  # MHC-10002, Kyle (south), none
GRACE = ("Grace Okafor", "512-555-0103")  # MHC-10003, Austin (central), plus, installed 2025-11-30
WREN = ("Wren Halvorsen", "512-555-0191")  # canary, Round Rock (north), standard
CONV = "conv-test"


def _session(c: sqlite3.Connection, who: tuple[str, str]) -> IdentitySession:
    s = IdentitySession()
    verify(c, s, who[0], phone=who[1])
    assert s.customer_id is not None
    return s


def _at(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setenv("APP_TODAY", when.date().isoformat())
    monkeypatch.setattr(clock, "now", lambda: when)


@pytest.fixture
def c(db: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    _at(monkeypatch, datetime(2026, 10, 16, 10, 0))
    conn = connect(db)
    conn.execute(
        "INSERT INTO conversations (id, started_at, visitor_key) VALUES (?, ?, 'v')", (CONV, "x")
    )
    conn.commit()
    return conn


def _write(c: sqlite3.Connection, sql: str, args: tuple[object, ...] = ()) -> None:
    c.execute(sql, args)
    c.commit()


def _slot_status(c: sqlite3.Connection, slot_id: int) -> str:
    return str(
        c.execute("SELECT status FROM appointment_slots WHERE slot_id = ?", (slot_id,)).fetchone()[
            0
        ]
    )


def _offer(
    c: sqlite3.Connection, s: IdentitySession, st_: SchedulingState, **kw: object
) -> list[Slot]:
    out = find_slots(c, s, st_, "hvac_repair", **kw)  # type: ignore[arg-type]
    assert isinstance(out, list), out
    return out


def test_slots_match_zone_service_membership_and_start_tomorrow(c: sqlite3.Connection) -> None:
    linda, omar = _session(c, LINDA), _session(c, OMAR)
    for s, zone, statuses in (
        (linda, "north", {"open", "reserved_members"}),
        (omar, "south", {"open"}),
    ):
        slots = _offer(c, s, SchedulingState())
        assert len(slots) == 3
        for x in slots:
            row = c.execute(
                "SELECT zone, service_type, status FROM appointment_slots WHERE slot_id = ?",
                (x.slot_id,),
            ).fetchone()
            assert (
                row["zone"] == zone
                and row["service_type"] == "hvac_repair"
                and row["status"] in statuses
            )
            assert x.date > date(2026, 10, 16) and x.tech_name
    # Linda (member) is offered the member-reserved slot when it's nearest; Omar never is
    _write(c, "UPDATE appointment_slots SET status = 'reserved_members' WHERE slot_id = 56")
    assert 56 in [x.slot_id for x in _offer(c, linda, SchedulingState())]
    omar_slots = _offer(c, omar, SchedulingState(), on=date(2026, 10, 19))
    assert all(_slot_status(c, x.slot_id) == "open" for x in omar_slots)


def test_evening_window_only_on_tuesday_and_thursday(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT INTO appointment_slots (slot_date, window, tech_id, zone, service_type, status) "
        "VALUES ('2026-10-19', '16:00-19:00', 1, 'north', 'hvac_repair', 'open')"  # a Monday
    )
    bad = int(c.execute("SELECT max(slot_id) FROM appointment_slots").fetchone()[0])
    slots = _offer(
        c, _session(c, LINDA), SchedulingState(), on=date(2026, 10, 19), window="16:00-19:00"
    )
    assert bad not in [x.slot_id for x in slots]


def test_exact_request_first_then_nearest_days(c: sqlite3.Connection) -> None:
    slots = _offer(
        c, _session(c, LINDA), SchedulingState(), on=date(2026, 10, 20), window="12:00-16:00"
    )
    assert (slots[0].date, slots[0].window) == (date(2026, 10, 20), "12:00-16:00")
    assert all(abs((x.date - date(2026, 10, 20)).days) <= 1 for x in slots)


def test_anything_earlier_only_offers_earlier_slots(c: sqlite3.Connection) -> None:
    limit = (date(2026, 10, 22), "08:00-12:00")
    slots = _offer(c, _session(c, LINDA), SchedulingState(), earlier_than=limit)
    assert slots and all((x.date, x.window) < limit for x in slots)
    assert slots == sorted(slots, key=lambda x: (x.date, x.window))


def test_unbookable_requests_hand_off(c: sqlite3.Connection) -> None:
    linda = _session(c, LINDA)
    assert (
        find_slots(c, linda, SchedulingState(), "hvac_repair", on=date(2026, 10, 16))
        is Handoff.SAME_DAY
    )
    assert (
        find_slots(c, linda, SchedulingState(), "plumbing") is Handoff.NO_SLOTS_FOR_SERVICE
    )  # I15
    grace = _session(c, GRACE)
    assert (
        find_slots(c, grace, SchedulingState(), "estimate") is Handoff.NO_SLOTS_FOR_SERVICE
    )  # I15
    _write(c, "UPDATE customers SET city = 'Manor' WHERE customer_id = 'MHC-00901'")
    assert (
        find_slots(c, _session(c, WREN), SchedulingState(), "hvac_repair") is Handoff.NO_ZONE
    )  # I14
    with pytest.raises(NotVerified):
        find_slots(c, IdentitySession(), SchedulingState(), "hvac_repair")


def test_book_only_an_offered_slot_and_confirm_details(c: sqlite3.Connection) -> None:
    grace, state = _session(c, GRACE), SchedulingState()
    with pytest.raises(NotOffered):
        book(c, grace, state, 56, "AC blowing warm")
    slots = _offer(c, grace, state)
    out = book(c, grace, state, slots[1].slot_id, "AC blowing warm")
    assert isinstance(out, Done) and out.appointment.slot == slots[1]
    assert out.appointment.is_warranty_visit  # installed 2025-11-30, inside 2 years
    row = c.execute(
        "SELECT * FROM appointments WHERE appointment_id = ?", (out.appointment.appointment_id,)
    ).fetchone()
    assert (row["customer_id"], row["status"], row["created_at"], row["is_warranty_visit"]) == (
        "MHC-10003",
        "scheduled",
        "2026-10-16",
        1,
    )
    assert _slot_status(c, slots[1].slot_id) == "booked" and state.offered == []
    linda, ls = _session(c, LINDA), SchedulingState()
    out = book(c, linda, ls, _offer(c, linda, ls)[0].slot_id, "noise")
    assert isinstance(out, Done) and not out.appointment.is_warranty_visit  # installed 2023


def test_two_concurrent_bookings_of_one_slot_exactly_one_wins(
    db: Path, c: sqlite3.Connection
) -> None:
    slot = _offer(c, _session(c, LINDA), SchedulingState())[0].slot_id
    results: list[object] = []
    barrier = threading.Barrier(2)

    def run(who: tuple[str, str]) -> None:
        conn = connect(db)
        s = _session(conn, who)
        barrier.wait()
        results.append(book(conn, s, SchedulingState(offered=[slot]), slot, "race"))
        conn.close()

    threads = [threading.Thread(target=run, args=(w,)) for w in (LINDA, WREN)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(type(r).__name__ for r in results) == ["Done", "SlotTaken"]
    n = c.execute(
        "SELECT count(*) FROM appointments WHERE slot_id = ? AND status = 'scheduled'", (slot,)
    ).fetchone()[0]
    assert n == 1


def test_linda_moves_her_visit_earlier_prd_example(c: sqlite3.Connection) -> None:
    linda, state = _session(c, LINDA), SchedulingState()
    [appt] = get_appointments(c, linda)
    assert (appt.slot.date, appt.slot.window, appt.slot.service_type) == (
        date(2026, 10, 22),
        "08:00-12:00",
        "hvac_repair",
    )
    earliest = _offer(c, linda, state, earlier_than=(appt.slot.date, appt.slot.window))[0]
    out = reschedule(c, linda, state, CONV, appt.appointment_id, earliest.slot_id)
    assert isinstance(out, Done) and out.fee_review_id is None
    assert out.appointment.slot.date < date(2026, 10, 22) and out.appointment.slot.tech_name
    assert (
        _slot_status(c, appt.slot.slot_id) == "open"
        and _slot_status(c, earliest.slot_id) == "booked"
    )
    assert [a.slot.slot_id for a in get_appointments(c, linda)] == [earliest.slot_id]


def test_change_inside_24h_needs_formal_consent_and_logs_a_fee_review(
    c: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, datetime(2026, 10, 21, 12, 0))  # Linda's window starts 10-22 08:00: 20 h away
    linda, state = _session(c, LINDA), SchedulingState()
    [appt] = get_appointments(c, linda)
    new = _offer(c, linda, state)[0].slot_id
    for consent in (
        None,
        FeeConsent("ok", "typed"),
        FeeConsent("fine, whatever", "typed"),
        FeeConsent("I agree", "button"),
    ):
        assert isinstance(
            reschedule(c, linda, state, CONV, appt.appointment_id, new, consent), NeedsFeeConsent
        )
    assert _slot_status(c, appt.slot.slot_id) == "booked"  # nothing changed
    out = reschedule(
        c, linda, state, CONV, appt.appointment_id, new, FeeConsent("i agree!", "typed")
    )
    assert isinstance(out, Done) and out.fee_review_id
    fee = c.execute("SELECT * FROM fee_reviews WHERE id = ?", (out.fee_review_id,)).fetchone()
    assert (
        fee["amount_cents"],
        fee["change"],
        fee["consent_text"],
        fee["consent_method"],
        fee["status"],
        fee["notice_text"],
    ) == (4900, "reschedule", "i agree!", "typed", "open", fee_notice())
    # no invoice is created or changed (rule 7)
    assert (
        c.execute("SELECT count(*) FROM invoices WHERE customer_id = 'MHC-10001'").fetchone()[0]
        == 0
    )


def test_cancel_frees_the_slot_late_cancel_needs_consent(
    c: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, datetime(2026, 10, 21, 12, 0))
    linda, state = _session(c, LINDA), SchedulingState()
    [appt] = get_appointments(c, linda)
    assert isinstance(cancel(c, linda, state, CONV, appt.appointment_id), NeedsFeeConsent)
    out = cancel(c, linda, state, CONV, appt.appointment_id, FeeConsent(fee_button(), "button"))
    assert isinstance(out, Done) and out.fee_review_id
    assert _slot_status(c, appt.slot.slot_id) == "open" and get_appointments(c, linda) == []
    with pytest.raises(NotFound):
        cancel(c, linda, SchedulingState(), CONV, appt.appointment_id)  # already cancelled


def test_early_cancel_has_no_fee(c: sqlite3.Connection) -> None:
    linda = _session(c, LINDA)
    [appt] = get_appointments(c, linda)
    out = cancel(c, linda, SchedulingState(), CONV, appt.appointment_id)
    assert isinstance(out, Done) and out.fee_review_id is None


def test_one_caller_cannot_touch_another_callers_appointment(c: sqlite3.Connection) -> None:
    omar, state = _session(c, OMAR), SchedulingState()
    assert get_appointments(c, omar) == []
    with pytest.raises(NotFound):
        cancel(c, omar, SchedulingState(), CONV, 159)  # Linda's appointment
    slot = _offer(c, omar, state)[0].slot_id
    with pytest.raises(NotFound):
        reschedule(c, omar, state, CONV, 159, slot)
    assert _slot_status(c, slot) == "open"


def test_reschedule_keeps_the_service_type(c: sqlite3.Connection) -> None:
    linda, state = _session(c, LINDA), SchedulingState()
    [appt] = get_appointments(c, linda)
    tune = find_slots(c, linda, state, "hvac_tune_up")
    assert isinstance(tune, list)
    with pytest.raises(NotOffered):
        reschedule(c, linda, state, CONV, appt.appointment_id, tune[0].slot_id)


def test_slot_taken_between_offer_and_booking(c: sqlite3.Connection) -> None:
    linda, state = _session(c, LINDA), SchedulingState()
    slot = _offer(c, linda, state)[0].slot_id
    c.execute("UPDATE appointment_slots SET status = 'booked' WHERE slot_id = ?", (slot,))
    c.commit()
    assert isinstance(book(c, linda, state, slot, "x"), SlotTaken)
    assert slot not in state.offered


def test_account_lookup_is_read_only_and_locked(c: sqlite3.Connection) -> None:
    omar, linda = _session(c, OMAR), _session(c, LINDA)
    [inv] = get_invoices(c, omar)
    assert (inv.invoice_id, inv.balance_cents) == ("INV-204411", 47800)
    assert get_invoices(c, linda) == []
    assert get_membership(c, linda)[0] == "standard" and get_membership(c, omar) == ("none", None)


def test_fee_is_read_from_the_kb_and_a_missing_row_stops_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert late_change_fee_cents() == 4900  # the 2026 fee schedule says $49
    assert fee_button() == "I agree to the $49 fee"
    from meridian.kb import manifest
    from meridian.scheduling import FEE_DOC

    fake = tmp_path / "fees.md"
    fake.write_text("| Fee | Non-member |\n|---|---|\n| Late cancellation (<24h) | $55.50 |\n")
    real = manifest.load()
    swapped = [
        manifest.Doc(d.path, d.status, d.audience, d.effective, fake) if d.path == FEE_DOC else d
        for d in real
    ]
    monkeypatch.setattr("meridian.scheduling.load_manifest", lambda: swapped)
    late_change_fee_cents.cache_clear()
    assert late_change_fee_cents() == 5550
    fake.write_text("| Fee | Non-member |\n")
    late_change_fee_cents.cache_clear()
    with pytest.raises(RuntimeError, match="late-cancellation"):
        late_change_fee_cents()
    late_change_fee_cents.cache_clear()


def _invariant_ok(c: sqlite3.Connection) -> None:
    bad = c.execute(
        "SELECT count(*) FROM appointments a JOIN appointment_slots s USING (slot_id) "
        "WHERE a.status = 'scheduled' AND s.status != 'booked'"
    ).fetchone()[0]
    double = c.execute(
        "SELECT count(*) FROM (SELECT slot_id FROM appointments WHERE status = 'scheduled' "
        "GROUP BY slot_id HAVING count(*) > 1)"
    ).fetchone()[0]
    orphan = c.execute(
        "SELECT count(*) FROM appointment_slots WHERE status = 'booked' AND slot_id NOT IN "
        "(SELECT slot_id FROM appointments WHERE status = 'scheduled')"
    ).fetchone()[0]
    assert (bad, double, orphan) == (0, 0, 0)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    ops=st.lists(
        st.tuples(
            st.sampled_from(["book", "move", "cancel"]), st.integers(0, 2), st.integers(0, 9)
        ),
        max_size=12,
    )
)
def test_slot_and_appointment_invariant_holds(
    ops: list[tuple[str, int, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No slot is open while a scheduled appointment points at it; no double or orphan bookings."""
    _at(monkeypatch, datetime(2026, 10, 16, 6, 0))
    tmp = tempfile.TemporaryDirectory()
    c = connect(init_db(Path(tmp.name) / "m.sqlite"))
    c.execute(
        "INSERT INTO conversations (id, started_at, visitor_key) VALUES (?, 'x', 'v')", (CONV,)
    )
    c.commit()
    people = [_session(c, w) for w in (LINDA, WREN, OMAR)]
    _invariant_ok(c)
    for op, who, pick in ops:
        s, state = people[who], SchedulingState()
        appts = get_appointments(c, s)
        if op == "cancel" and appts:
            cancel(c, s, state, CONV, appts[pick % len(appts)].appointment_id)
        else:
            svc = appts[0].slot.service_type if (op == "move" and appts) else "hvac_repair"
            slots = find_slots(c, s, state, svc, earlier_than=None)
            if isinstance(slots, list):
                target = slots[pick % len(slots)].slot_id
                if op == "move" and appts:
                    reschedule(c, s, state, CONV, appts[0].appointment_id, target)
                else:
                    book(c, s, state, target, "prop")
        _invariant_ok(c)
    c.close()
    tmp.cleanup()


def test_consent_only_counts_for_the_change_it_was_asked_for(
    c: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, datetime(2026, 10, 21, 12, 0))
    linda, state = _session(c, LINDA), SchedulingState()
    [appt] = get_appointments(c, linda)
    button = FeeConsent(fee_button(), "button")
    # consent with no notice shown first is not consent
    assert isinstance(cancel(c, linda, state, CONV, appt.appointment_id, button), NeedsFeeConsent)
    new = _offer(c, linda, state)[0].slot_id
    # the pending notice is for the cancel; a reschedule needs its own
    out = reschedule(c, linda, state, CONV, appt.appointment_id, new, button)
    assert isinstance(out, NeedsFeeConsent) and _slot_status(c, appt.slot.slot_id) == "booked"
    assert isinstance(reschedule(c, linda, state, CONV, appt.appointment_id, new, button), Done)
    assert state.pending_fee is None


def test_same_day_or_started_visits_hand_off(
    c: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, datetime(2026, 10, 22, 9, 0))  # Linda's 08:00-12:00 visit has started
    linda, state = _session(c, LINDA), SchedulingState()
    [appt] = get_appointments(c, linda)
    assert cancel(c, linda, state, CONV, appt.appointment_id) is Handoff.SAME_DAY
    assert reschedule(c, linda, state, CONV, appt.appointment_id, 1) is Handoff.SAME_DAY
    assert _slot_status(c, appt.slot.slot_id) == "booked"


def test_nothing_earlier_is_an_answer_not_a_handoff(c: sqlite3.Connection) -> None:
    out = find_slots(
        c,
        _session(c, LINDA),
        SchedulingState(),
        "hvac_repair",
        earlier_than=(date(2026, 10, 17), "08:00-12:00"),
    )
    assert out == []


@pytest.mark.parametrize(("hour", "minute", "late"), [(8, 0, False), (8, 1, True), (7, 59, False)])
def test_24_hour_boundary(
    c: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, hour: int, minute: int, late: bool
) -> None:
    _at(monkeypatch, datetime(2026, 10, 21, hour, minute))  # Linda's window: 10-22 08:00
    linda = _session(c, LINDA)
    [appt] = get_appointments(c, linda)
    out = cancel(c, linda, SchedulingState(), CONV, appt.appointment_id)
    assert isinstance(out, NeedsFeeConsent) is late


@pytest.mark.parametrize(
    ("today", "install", "warranty"),
    [
        ("2026-10-16", "2024-10-16", False),
        ("2026-10-16", "2024-10-17", True),
        ("2028-02-29", "2026-02-28", False),
        ("2028-02-29", "2026-03-01", True),
    ],
)
def test_warranty_is_strictly_under_two_years(
    monkeypatch: pytest.MonkeyPatch, today: str, install: str, warranty: bool
) -> None:
    from meridian.scheduling import _warranty  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("APP_TODAY", today)
    assert _warranty(install) is warranty
