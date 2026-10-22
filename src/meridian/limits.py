"""Public-URL limits (D14) and the identity-guessing cap (T7 review). DB writes allowed (rule 4).

A visitor is their IP address: cookies are free to drop, so every limit keys on the IP alone.
Every limit hit is recorded (rate_events + usage_daily.limit_hits) for the dashboard (D15).
"""

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from meridian import clock

MAX_MESSAGE_CHARS = 1000
PER_MINUTE = 20
PER_HOUR = 150
TURNS_PER_CONVERSATION = 30
CONVERSATIONS_PER_DAY = 30
BOOKINGS_PER_DAY = 10
IDENTITY_FAILS_PER_DAY = 5
DAILY_SPEND_CAP_USD_MICROS = 15_000_000  # $15/day, owner (D14.2)

UNAVAILABLE = (
    "Our virtual assistant is unavailable right now. Please try again later or contact Meridian "
    "by phone during business hours."
)
MESSAGES = {
    "message_too_long": f"That message is too long. Please keep it under {MAX_MESSAGE_CHARS} "
    "characters.",
    "messages_per_minute": "You're sending messages quickly. Please wait a minute and try again.",
    "messages_per_hour": "You've reached the message limit for now. Please try again later.",
    "conversations_per_day": "You've reached today's limit for new chats. Please try tomorrow.",
    "turns_per_conversation": (
        "This chat has reached its length limit. Please start a new chat, or call Meridian during "
        "business hours."
    ),
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""  # key into MESSAGES, or "spend_cap"
    message: str = ""


def _since(conn: sqlite3.Connection, visitor: str, kind: str, delta: timedelta) -> int:
    cutoff = (clock.ops_now() - delta).isoformat()
    row = conn.execute(
        "SELECT count(*) FROM rate_events WHERE visitor_key = ? AND kind = ? AND at >= ?",
        (visitor, kind, cutoff),
    ).fetchone()
    return int(row[0])


def record(conn: sqlite3.Connection, visitor: str, kind: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO rate_events (visitor_key, kind, at) VALUES (?, ?, ?)",
            (visitor, kind, clock.ops_now().isoformat()),
        )


def _usage_row(conn: sqlite3.Connection) -> str:
    day = clock.ops_date().isoformat()
    with conn:
        if conn.execute("INSERT OR IGNORE INTO usage_daily (date) VALUES (?)", (day,)).rowcount:
            cutoff = (
                clock.ops_now() - timedelta(days=2)
            ).isoformat()  # a new day: prune old events
            conn.execute("DELETE FROM rate_events WHERE at < ?", (cutoff,))
    return day


def spend_today(conn: sqlite3.Connection) -> int:
    day = _usage_row(conn)
    return int(
        conn.execute("SELECT cost_usd_micros FROM usage_daily WHERE date = ?", (day,)).fetchone()[0]
    )


def add_spend(conn: sqlite3.Connection, usd_micros: int, new_conversation: bool = False) -> None:
    day = _usage_row(conn)
    with conn:
        conn.execute(
            "UPDATE usage_daily SET cost_usd_micros = cost_usd_micros + ?, "
            "conversations = conversations + ? WHERE date = ?",
            (usd_micros, int(new_conversation), day),
        )


def count_hit(conn: sqlite3.Connection, visitor: str, reason: str) -> None:
    """Count a refusal (D14.5). At most one event row per visitor, reason and minute, so a flood
    of refused requests can't fill the disk; the daily counter still counts every hit."""
    day = _usage_row(conn)
    with conn:
        conn.execute("UPDATE usage_daily SET limit_hits = limit_hits + 1 WHERE date = ?", (day,))
    if not _since(conn, visitor, f"limit:{reason}", timedelta(minutes=1)):
        record(conn, visitor, f"limit:{reason}")


def _hit(conn: sqlite3.Connection, visitor: str, reason: str) -> Verdict:
    count_hit(conn, visitor, reason)
    return Verdict(False, reason, UNAVAILABLE if reason == "spend_cap" else MESSAGES[reason])


def check_message(
    conn: sqlite3.Connection, visitor: str, text: str, turns_so_far: int, new_conversation: bool
) -> Verdict:
    """Run before any model call. Records the message (or the hit) as a rate event."""
    if spend_today(conn) >= DAILY_SPEND_CAP_USD_MICROS:
        return _hit(conn, visitor, "spend_cap")
    if len(text) > MAX_MESSAGE_CHARS:
        return _hit(conn, visitor, "message_too_long")
    if _since(conn, visitor, "message", timedelta(minutes=1)) >= PER_MINUTE:
        return _hit(conn, visitor, "messages_per_minute")
    if _since(conn, visitor, "message", timedelta(hours=1)) >= PER_HOUR:
        return _hit(conn, visitor, "messages_per_hour")
    if (
        new_conversation
        and _since(conn, visitor, "conversation", timedelta(days=1)) >= CONVERSATIONS_PER_DAY
    ):
        return _hit(conn, visitor, "conversations_per_day")
    if turns_so_far >= TURNS_PER_CONVERSATION:
        return _hit(conn, visitor, "turns_per_conversation")
    record(conn, visitor, "message")
    if new_conversation:
        record(conn, visitor, "conversation")
    return Verdict(True)


def unavailable(conn: sqlite3.Connection) -> bool:
    return spend_today(conn) >= DAILY_SPEND_CAP_USD_MICROS


def bookings_left(conn: sqlite3.Connection, visitor: str) -> bool:
    return _since(conn, visitor, "booking", timedelta(days=1)) < BOOKINGS_PER_DAY


def identity_blocked(conn: sqlite3.Connection, visitor: str) -> bool:
    return _since(conn, visitor, "identity_fail", timedelta(days=1)) >= IDENTITY_FAILS_PER_DAY
