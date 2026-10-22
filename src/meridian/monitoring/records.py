"""Conversation, turn and handoff records (D11, D15). Monitoring timestamps are real time
(`clock.ops_now`), not the frozen demo date, so "abandoned after 30 minutes" works."""

import json
import re
import sqlite3

from meridian import clock, feedback
from meridian.agent.handoff import URGENCY, Reason, Ticket
from meridian.agent.state import Conversation, Turn


def ensure_conversation(conn: sqlite3.Connection, conversation_id: str, visitor_key: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversations (id, started_at, visitor_key) VALUES (?, ?, ?)",
            (conversation_id, clock.ops_now().isoformat(), visitor_key),
        )


def create_ticket(
    conn: sqlite3.Connection,
    conversation_id: str,
    reason: Reason,
    customer_id: str | None,
    summary: str,
    tried: str,
    wants: str,
) -> Ticket:
    """Saves a handoff ticket to the staff queue (D11.5); the conversation is in turn_events."""
    urgency = URGENCY.get(reason, "standard")
    t = Ticket(
        0,
        reason,
        urgency,
        customer_id,
        customer_id is not None,
        summary[:600],
        tried[:600],
        wants[:300],
    )
    with conn:
        cur = conn.execute(
            "INSERT INTO handoff_tickets (conversation_id, reason, urgency, customer_id, verified, "
            "summary, tried, wants, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                conversation_id,
                reason.value,
                urgency,
                customer_id,
                int(t.verified),
                t.summary,
                t.tried,
                t.wants,
                clock.ops_now().isoformat(),
            ),
        )
    return Ticket(
        int(cur.lastrowid or 0),
        *(
            getattr(t, f)
            for f in ("reason", "urgency", "customer_id", "verified", "summary", "tried", "wants")
        ),
    )


def set_callback_window(conn: sqlite3.Connection, ticket_id: int, window: str) -> None:
    with conn:
        conn.execute(
            "UPDATE handoff_tickets SET callback_window = ? WHERE id = ?", (window[:120], ticket_id)
        )


def set_rating(conn: sqlite3.Connection, conversation_id: str, rating: int) -> None:
    """The caller's 👍 (1) / 👎 (-1) for a conversation (D15, D16)."""
    with conn:
        cur = conn.execute(
            "UPDATE conversations SET rating = ? WHERE id = ?", (rating, conversation_id)
        )
        if rating == -1 and cur.rowcount == 1:  # logs may have been deleted mid-chat
            feedback.flag(conn, conversation_id, "thumbs_down")


_CLOSER = re.compile(r"anything else|other questions?|help (you )?with anything|something else")


def _asks_caller(reply: str) -> bool:
    """The reply ends by asking the caller something other than "anything else?"."""
    last = re.split(r"(?<=[.!?])\s+", reply.strip())[-1] if reply.strip() else ""
    return last.endswith("?") and not _CLOSER.search(last.lower())


def outcome_of(conv: Conversation, turn: Turn, fee_followup: bool) -> tuple[str, str]:
    """(outcome, reason) after this turn (D15.2). "in_progress" becomes "abandoned" in the
    metrics once 30 minutes pass with no new message, so a caller who leaves while the
    assistant waits on them never counts as contained.
    shortcut: "waiting" is read from pending state and a trailing question; an explicit
    done-signal from the workers would be exact (upgrade if the eval shows misreads)."""
    if turn.route == "error":
        return "failed", "internal_error"
    if turn.ticket is not None:
        reason = turn.ticket.reason.value
        return ("failed" if reason == "assistant_unavailable" else "handed_off"), reason
    if (
        conv.offered_slots
        or conv.readback is not None
        or conv.sched.pending_fee
        or conv.pending_cancel
        or conv.callback_ticket is not None
        or (turn.route == "scheduling" and conv.identity.customer_id is None)
        or _asks_caller(turn.reply)
    ):
        return "in_progress", "waiting_on_caller"
    return ("contained_fee_followup" if fee_followup else "contained"), ""


def record_turn(
    conn: sqlite3.Connection, conv: Conversation, user_text: str, turn: Turn, latency_ms: int
) -> None:
    """One caller message and its reply, with what monitoring needs (D15.1, D15.5). The full
    text is kept here for staff review (D17.1); it never goes to the logs."""
    calls = turn.model_calls
    cost = sum(c.cost_usd_micros for c in calls)
    ensure_conversation(conn, conv.id, conv.visitor_key)  # staff may have deleted logs mid-turn
    with conn:
        seq = int(
            conn.execute(
                "SELECT count(*) FROM turn_events WHERE conversation_id = ?", (conv.id,)
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO turn_events (conversation_id, seq, role, text) VALUES (?, ?, 'user', ?)",
            (conv.id, seq + 1, user_text),
        )
        conn.execute(
            "INSERT INTO turn_events (conversation_id, seq, role, text, worker, tool_calls, "
            "sections, check_result, latency_ms, model, tokens_in, tokens_out, cost_usd_micros) "
            "VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conv.id,
                seq + 2,
                turn.reply,
                turn.route,
                json.dumps(turn.tool_calls),
                json.dumps(sorted(turn.sections)),
                ",".join(turn.check_failures) or None,
                latency_ms,
                ",".join(sorted({c.model for c in calls})) or None,
                sum(c.usage.input + c.usage.cache_read + c.usage.cache_write for c in calls),
                sum(c.usage.output for c in calls),
                cost,
            ),
        )
        row = conn.execute(
            "SELECT outcome, models_used FROM conversations WHERE id = ?", (conv.id,)
        ).fetchone()
        fee = conn.execute(
            "SELECT 1 FROM fee_reviews WHERE conversation_id = ? LIMIT 1", (conv.id,)
        ).fetchone()
        outcome, reason = outcome_of(conv, turn, fee is not None)
        if (
            row["outcome"] in ("handed_off", "failed") and outcome != "failed"
        ):  # a handoff stays the conversation's outcome
            outcome, reason = row["outcome"], None
        models = sorted(set(json.loads(row["models_used"] or "[]")) | {c.model for c in calls})
        conn.execute(
            "UPDATE conversations SET ended_at = ?, turns = turns + 1, "
            "cost_usd_micros = cost_usd_micros + ?, customer_id = COALESCE(customer_id, ?), "
            "outcome = ?, outcome_reason = COALESCE(?, outcome_reason), models_used = ? "
            "WHERE id = ?",
            (
                clock.ops_now().isoformat(),
                cost,
                conv.identity.customer_id,
                outcome,
                reason,
                json.dumps(models),
                conv.id,
            ),
        )
        feedback.flag_turn(conn, conv.id, user_text, turn)


def close_fee_review(conn: sqlite3.Connection, fee_id: int) -> None:
    with conn:
        conn.execute("UPDATE fee_reviews SET status = 'reviewed' WHERE id = ?", (fee_id,))
