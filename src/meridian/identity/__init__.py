"""Caller identity (D9): name + one of account number / phone / service address, both pointing
to the same customer. Two failed attempts hand off. Once verified, the session is locked to that
customer; account tools get the customer ID from here, never from model arguments (rule 6).

Nothing here tells the caller *what* didn't match: every failure gets the same message (D9.4).
"""

import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

MAX_FAILURES = 2
NAME_SIMILARITY = (
    0.8  # per name part (first, last): forgives small typos, not a different first name
)

_FACTORS = "your account number, the phone number on the account, or the service address"
ASK_FACTOR = f"Could you give me {_FACTORS}?"
NO_MATCH = f"That doesn't match what I have. Could you try {_FACTORS}?"
VERIFIED = "Thanks, you're verified."
HANDOFF = "I'm not able to verify the account here, so I'll pass you to a team member who can help."

_SUFFIXES = {
    "avenue": "ave", "street": "st", "drive": "dr", "court": "ct", "boulevard": "blvd",
    "lane": "ln", "road": "rd", "terrace": "ter", "terr": "ter", "parkway": "pkwy",
}  # fmt: skip


class Outcome(Enum):
    ASK_FACTOR = "ask_factor"  # no second factor given yet (said whether or not the name exists)
    NO_MATCH = "no_match"  # counts as a failed attempt
    VERIFIED = "verified"
    HANDOFF = "handoff"  # 2 failures, still ambiguous, or a second identity in a locked session


class NotVerified(Exception):
    """An account tool was called before the caller was verified."""


@dataclass
class IdentitySession:
    customer_id: str | None = None  # written only by verify() (rule 6)
    failures: int = 0
    handed_off: bool = False  # once handed off, no more attempts this conversation

    def require_customer(self) -> str:
        """The only way account tools learn whose records to touch (rule 6)."""
        if self.customer_id is None:
            raise NotVerified
        return self.customer_id


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    message: str


_TITLES = frozenset({"mr", "mrs", "ms", "miss", "dr", "jr", "sr", "ii", "iii"})


def _name(s: str) -> str:
    """Lowercase words, titles/suffixes dropped; "Patel, Omar" becomes "omar patel"."""
    if s.count(",") == 1:
        last, first = s.split(",")
        s = f"{first} {last}"
    return " ".join(w for w in re.sub(r"[^a-z ]", " ", s.lower()).split() if w not in _TITLES)


def _digits(s: str) -> str:
    """Phone: digits only, dropping a leading US country code."""
    d = re.sub(r"\D", "", s)
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def _account(s: str) -> str:
    """'MHC-10001', 'mhc 10001' and '10001' are the same account number."""
    return re.sub(r"[\s-]", "", s.upper()).removeprefix("MHC")


def _address(s: str) -> list[str]:
    words = re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()
    return [_SUFFIXES.get(w, w) for w in words]


def _address_matches(given: str, row: sqlite3.Row) -> bool:
    """Exact street address after normalizing; only the city, ZIP or state may follow it."""
    g, f = _address(given), _address(str(row["service_address"]))
    allowed_tail = {*_address(str(row["city"])), str(row["zip"]), "tx", "texas"}
    return len(f) > 0 and g[: len(f)] == f and set(g[len(f) :]) <= allowed_tail


def _name_matches(given: str, on_file: str) -> bool:
    """First and last name each match, forgiving small typos ("Lynda" = "Linda")."""
    g, f = _name(given).split(), _name(on_file).split()
    if len(g) < 2 or len(f) < 2:
        return False
    return all(
        SequenceMatcher(None, a, b).ratio() >= NAME_SIMILARITY
        for a, b in ((g[0], f[0]), (g[-1], f[-1]))
    )


def _factor_matches(
    row: sqlite3.Row, account: str | None, phone: str | None, address: str | None
) -> bool:
    return all(
        (
            account is None or _account(account) == _account(row["customer_id"]),
            phone is None
            or (len(_digits(phone)) == 10 and _digits(phone) == _digits(row["phone"])),
            address is None or _address_matches(address, row),
        )
    )


def verify(
    conn: sqlite3.Connection,
    session: IdentitySession,
    name: str,
    account: str | None = None,
    phone: str | None = None,
    address: str | None = None,
) -> Result:
    """One verification attempt. Updates the session; returns what to tell the caller."""
    if session.handed_off:
        return Result(Outcome.HANDOFF, HANDOFF)
    if not any(x and x.strip() for x in (account, phone, address)):
        return Result(Outcome.ASK_FACTOR, ASK_FACTOR)
    account, phone, address = (x if x and x.strip() else None for x in (account, phone, address))
    matches = [
        r
        for r in conn.execute(
            "SELECT customer_id, name, phone, service_address, city, zip FROM customers"
        )
        if _name_matches(name, str(r["name"])) and _factor_matches(r, account, phone, address)
    ]
    if len(matches) == 1 and session.customer_id in (None, str(matches[0]["customer_id"])):
        session.customer_id = str(matches[0]["customer_id"])
        return Result(Outcome.VERIFIED, VERIFIED)
    if matches:  # still ambiguous, or a second identity in a locked conversation
        session.handed_off = True
        return Result(Outcome.HANDOFF, HANDOFF)
    session.failures += 1
    if session.failures >= MAX_FAILURES:
        session.handed_off = True
        return Result(Outcome.HANDOFF, HANDOFF)
    return Result(Outcome.NO_MATCH, NO_MATCH)


def current_customer(conn: sqlite3.Connection, session: IdentitySession) -> sqlite3.Row:
    """The verified caller's own record. Takes no customer ID by design (rule 6)."""
    row = conn.execute(
        "SELECT * FROM customers WHERE customer_id = ?", (session.require_customer(),)
    ).fetchone()
    if row is None:
        raise NotVerified
    return row
