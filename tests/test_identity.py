import inspect
from pathlib import Path

import pytest

from meridian.db import connect
from meridian.identity import (
    HANDOFF,
    NO_MATCH,
    IdentitySession,
    NotVerified,
    Outcome,
    current_customer,
    verify,
)


def test_each_factor_verifies_with_forgiving_name_and_formatting(db: Path) -> None:
    with connect(db) as c:
        for kwargs in (
            {"phone": "(512) 555 0101"},
            {"phone": "+1 512-555-0101"},
            {"account": "mhc 10001"},
            {"account": "10001"},
        ):
            s = IdentitySession()
            assert verify(c, s, "linda foster", **kwargs).outcome is Outcome.VERIFIED, kwargs
            assert s.customer_id == "MHC-10001"
        addr = c.execute(
            "SELECT service_address, city FROM customers WHERE customer_id = 'MHC-10001'"
        ).fetchone()
        long_form = (
            addr[0].replace(" Ave", " Avenue").replace(" St", " Street").replace(" Dr", " Drive")
        )
        s = IdentitySession()
        assert (
            verify(c, s, "Lynda Foster", address=f"{long_form}, {addr[1]}").outcome
            is Outcome.VERIFIED
        )


def test_name_alone_asks_for_a_factor_whether_or_not_the_name_exists(db: Path) -> None:
    with connect(db) as c:
        a, b = IdentitySession(), IdentitySession()
        assert verify(c, a, "Linda Foster").message == verify(c, b, "Nobody Realname").message
        assert a.failures == b.failures == 0


@pytest.mark.parametrize(
    ("cid", "kwargs"),
    [
        ("MHC-97051", {"phone": "512-491-7320"}),
        ("MHC-66681", {"address": "5397 Lakeshore Boulevard, Buda TX 78610"}),
        ("MHC-70824", {"account": "MHC-70824"}),
    ],
)
def test_each_robert_patel_resolves_to_himself(db: Path, cid: str, kwargs: dict[str, str]) -> None:
    with connect(db) as c:
        s = IdentitySession()
        assert verify(c, s, "Robert Patel", phone="512-000-0000").outcome is Outcome.NO_MATCH
        assert verify(c, s, "robert patel", **kwargs).outcome is Outcome.VERIFIED
        assert s.customer_id == cid


def test_still_ambiguous_after_the_factor_hands_off_for_good(db: Path) -> None:
    with connect(db) as c:
        c.execute(
            "INSERT INTO customers (customer_id, name, phone, service_address, city, zip) "
            "VALUES ('MHC-00999', 'Robert Patel', '512-555-0199', '6046 Windcrest Ave', "
            "'Leander', '78641')"
        )
        s = IdentitySession()
        r = verify(c, s, "Robert Patel", address="6046 Windcrest Ave")
        assert r.outcome is Outcome.HANDOFF and s.customer_id is None and s.handed_off
        assert verify(c, s, "Robert Patel", account="MHC-70824").outcome is Outcome.HANDOFF


def test_a_different_first_name_with_the_same_surname_does_not_verify(db: Path) -> None:
    with connect(db) as c:
        for name in ("Maria Patel", "Omar", "Patel"):
            assert (
                verify(c, IdentitySession(), name, phone="512-555-0102").outcome is Outcome.NO_MATCH
            )
        assert (
            verify(c, IdentitySession(), "Omar Pate", phone="512-555-0102").outcome
            is Outcome.VERIFIED
        )
        for name in ("Dr Omar Patel", "Omar Patel Jr.", "patel, omar"):
            assert (
                verify(c, IdentitySession(), name, phone="512-555-0102").outcome is Outcome.VERIFIED
            )


def test_address_may_end_with_city_or_zip_but_nothing_else(db: Path) -> None:
    with connect(db) as c:
        street = c.execute(
            "SELECT service_address FROM customers WHERE customer_id = 'MHC-10001'"
        ).fetchone()[0]
        ok = verify(c, IdentitySession(), "Linda Foster", address=f"{street}, Round Rock, TX 78664")
        assert ok.outcome is Outcome.VERIFIED
        bad = verify(c, IdentitySession(), "Linda Foster", address=f"{street} garbage words")
        assert bad.outcome is Outcome.NO_MATCH


def test_wrong_factor_message_reveals_nothing(db: Path) -> None:
    with connect(db) as c:
        msgs = {
            verify(
                c, IdentitySession(), "Linda Foster", phone="512-555-0102"
            ).message,  # Omar's phone
            verify(c, IdentitySession(), "Nobody Realname", phone="512-555-0101").message,
            verify(c, IdentitySession(), "Linda Foster", account="MHC-99999").message,
        }
        assert msgs == {NO_MATCH}
        assert "0101" not in NO_MATCH and "Foster" not in NO_MATCH


def test_two_failures_hand_off_and_block_further_guesses(db: Path) -> None:
    with connect(db) as c:
        s = IdentitySession()
        assert verify(c, s, "Linda Foster", phone="512-555-0000").outcome is Outcome.NO_MATCH
        assert verify(c, s, "Linda Foster", phone="512-555-0001").outcome is Outcome.HANDOFF
        r = verify(c, s, "Linda Foster", phone="512-555-0101")  # right answer, too late
        assert r.outcome is Outcome.HANDOFF and r.message == HANDOFF and s.customer_id is None


def test_partial_phone_never_matches(db: Path) -> None:
    with connect(db) as c:
        assert (
            verify(c, IdentitySession(), "Linda Foster", phone="0101").outcome is Outcome.NO_MATCH
        )


def test_locked_session_cannot_read_or_switch_to_another_customer(db: Path) -> None:
    with connect(db) as c:
        s = IdentitySession()
        assert verify(c, s, "Omar Patel", phone="512-555-0102").outcome is Outcome.VERIFIED
        assert current_customer(c, s)["customer_id"] == "MHC-10002"
        r = verify(c, s, "Linda Foster", phone="512-555-0101")
        assert r.outcome is Outcome.HANDOFF and s.customer_id == "MHC-10002" and s.handed_off
        assert current_customer(c, s)["customer_id"] == "MHC-10002"
    assert list(inspect.signature(current_customer).parameters) == ["conn", "session"]


def test_unverified_session_reads_nothing(db: Path) -> None:
    with connect(db) as c, pytest.raises(NotVerified):
        current_customer(c, IdentitySession())


def test_canary_customers_verify_like_anyone_else(db: Path) -> None:
    with connect(db) as c:
        s = IdentitySession()
        assert verify(c, s, "Wren Halvorsen", phone="512-555-0191").outcome is Outcome.VERIFIED
        assert s.customer_id == "MHC-00901"
