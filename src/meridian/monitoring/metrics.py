"""Dashboard numbers (D15.4), computed from our own tables. Read-only.

Days are Central-time calendar days of real activity (ops time), not the frozen demo date.
"""

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from meridian import clock
from meridian.limits import DAILY_SPEND_CAP_USD_MICROS

ABANDON_AFTER = timedelta(minutes=30)
CONTAINED = ("contained", "contained_fee_followup")
FINISHED = (*CONTAINED, "handed_off", "failed", "abandoned")


def final_outcome(outcome: str | None, ended_at: str | None, now: datetime) -> str:
    """D15.2: left waiting on the caller for 30 minutes = abandoned, never contained."""
    if outcome == "in_progress" or outcome is None:
        last = datetime.fromisoformat(ended_at) if ended_at else None
        if last is None or now - last >= ABANDON_AFTER:
            return "abandoned"
        return "in_progress"
    return outcome


def _day(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone(clock.CENTRAL).date()


@dataclass
class DayStats:
    day: date
    conversations: int = 0
    finished: int = 0
    contained: int = 0
    fee_followup: int = 0
    handed_off: int = 0
    failed: int = 0
    abandoned: int = 0
    thumbs_down: int = 0
    contained_unhappy: int = 0
    handoff_reasons: Counter[str] = field(default_factory=Counter[str])

    @property
    def containment(self) -> float | None:
        return self.contained / self.finished if self.finished else None


@dataclass
class Overview:
    today: DayStats
    trend: list[DayStats]
    p50_ms: int | None
    p95_ms: int | None
    spend_usd_micros: int
    cap_usd_micros: int
    limit_hits: int
    check_failures: int


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def overview(conn: sqlite3.Connection, now: datetime | None = None, days: int = 14) -> Overview:
    now = now or datetime.now(UTC)
    today = now.astimezone(clock.CENTRAL).date()
    first = today - timedelta(days=days - 1)
    stats = {first + timedelta(days=i): DayStats(first + timedelta(days=i)) for i in range(days)}
    since = (now - timedelta(days=days + 1)).isoformat()
    rows = conn.execute(
        "SELECT started_at, ended_at, outcome, outcome_reason, rating FROM conversations "
        "WHERE started_at >= ?",
        (since,),
    ).fetchall()
    for r in rows:
        st = stats.get(_day(r["started_at"]))
        if st is None:
            continue
        out = final_outcome(r["outcome"], r["ended_at"], now)
        st.conversations += 1
        if out in FINISHED:
            st.finished += 1
        if out in CONTAINED:
            st.contained += 1
            st.fee_followup += out == "contained_fee_followup"
            st.contained_unhappy += r["rating"] == -1
        elif out == "handed_off":
            st.handed_off += 1
            st.handoff_reasons[str(r["outcome_reason"])] += 1
        elif out == "failed":
            st.failed += 1
        elif out == "abandoned":
            st.abandoned += 1
        st.thumbs_down += r["rating"] == -1
    start_today = (
        datetime.combine(today, datetime.min.time(), clock.CENTRAL).astimezone(UTC).isoformat()
    )
    lat = [
        int(r[0])
        for r in conn.execute(
            "SELECT e.latency_ms FROM turn_events e "
            "JOIN conversations c ON c.id = e.conversation_id "
            "WHERE e.role = 'assistant' AND e.latency_ms IS NOT NULL AND c.started_at >= ?",
            (start_today,),
        )
    ]
    checks = conn.execute(
        "SELECT count(*) FROM turn_events e JOIN conversations c ON c.id = e.conversation_id "
        "WHERE e.check_result IS NOT NULL AND c.started_at >= ?",
        (start_today,),
    ).fetchone()[0]
    usage = conn.execute(
        "SELECT cost_usd_micros, limit_hits FROM usage_daily WHERE date = ?", (today.isoformat(),)
    ).fetchone()
    return Overview(
        stats[today],
        list(stats.values()),
        _pct(lat, 0.5),
        _pct(lat, 0.95),
        int(usage[0]) if usage else 0,
        DAILY_SPEND_CAP_USD_MICROS,
        int(usage[1]) if usage else 0,
        int(checks),
    )


def recent_problems(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """👎 ratings, answer-check failures and failed conversations, newest first."""
    rows = conn.execute(
        "SELECT c.id, c.ended_at, c.outcome, c.outcome_reason, c.rating, "
        "(SELECT group_concat(e.check_result) FROM turn_events e "
        " WHERE e.conversation_id = c.id AND e.check_result IS NOT NULL) AS checks "
        "FROM conversations c WHERE c.rating = -1 OR c.outcome = 'failed' OR EXISTS "
        "(SELECT 1 FROM turn_events e "
        " WHERE e.conversation_id = c.id AND e.check_result IS NOT NULL) "
        "ORDER BY c.ended_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        why = [
            w
            for w in (
                "👎" if r["rating"] == -1 else "",
                r["outcome"] if r["outcome"] == "failed" else "",
            )
            if w
        ]
        if r["checks"]:
            why.append(f"answer check: {r['checks']}")
        out.append({"id": r["id"], "when": r["ended_at"], "why": "; ".join(why)})
    return out


def mask_phone(phone: str | None) -> str:
    digits = "".join(ch for ch in phone or "" if ch.isdigit())
    return f"{digits[:3]}-***-{digits[-4:]}" if len(digits) >= 7 else "—"


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"(?<!\d)\(?(\d{3})\)?[\s.-]?\d{3}[\s.-]?(\d{4})(?!\d)")


def mask_text(text: str | None) -> str:
    """Phones and emails hidden in free text shown in lists (summaries quote the caller)."""
    text = EMAIL.sub("[email]", text or "")
    return PHONE.sub(r"\1-***-\2", text)


def mask_name(name: str | None) -> str:
    parts = (name or "").split()
    return f"{parts[0][0]}. {parts[-1]}" if len(parts) >= 2 else (name or "—")


def handoff_queue(conn: sqlite3.Connection, status: str = "open") -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.id, t.conversation_id, t.reason, t.urgency, t.customer_id, t.summary, "
        "t.callback_window, t.created_at, cu.name, cu.phone FROM handoff_tickets t "
        "LEFT JOIN customers cu ON cu.customer_id = t.customer_id WHERE t.status = ? "
        "ORDER BY CASE t.urgency WHEN 'emergency' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END, "
        "t.id DESC",
        (status,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "reason": str(r["reason"]).replace("_", " "),
            "urgency": r["urgency"],
            "caller": f"{mask_name(r['name'])} {mask_phone(r['phone'])}"
            if r["customer_id"]
            else "not verified",
            "summary": mask_text(r["summary"]),
            "callback": mask_text(r["callback_window"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def fee_queue(conn: sqlite3.Connection, status: str = "open") -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT f.id, f.conversation_id, f.appointment_id, f.change, f.amount_cents, "
        "f.consent_method, f.consent_at, cu.name FROM fee_reviews f "
        "JOIN appointments a ON a.appointment_id = f.appointment_id "
        "JOIN customers cu ON cu.customer_id = a.customer_id WHERE f.status = ? ORDER BY f.id DESC",
        (status,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "visit": r["appointment_id"],
            "change": r["change"],
            "amount": f"${r['amount_cents'] / 100:,.2f}",
            "consent": r["consent_method"],
            "when": r["consent_at"],
            "caller": mask_name(r["name"]),
        }
        for r in rows
    ]


def conversation_detail(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any] | None:
    """Everything about one conversation for the staff detail page (D15.5). Unmasked: staff only."""
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if conv is None:
        return None
    events = [
        dict(r)
        | {
            "tool_calls": json.loads(r["tool_calls"] or "[]"),
            "sections": json.loads(r["sections"] or "[]"),
        }
        for r in conn.execute(
            "SELECT * FROM turn_events WHERE conversation_id = ? ORDER BY seq", (conversation_id,)
        )
    ]
    tickets = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM handoff_tickets WHERE conversation_id = ?", (conversation_id,)
        )
    ]
    fees = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM fee_reviews WHERE conversation_id = ?", (conversation_id,)
        )
    ]
    now = datetime.now(UTC)
    return {
        "conversation": dict(conv)
        | {"final_outcome": final_outcome(conv["outcome"], conv["ended_at"], now)},
        "events": events,
        "tickets": tickets,
        "fees": fees,
    }


def recent_conversations(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return [
        {
            "id": r["id"],
            "started_at": r["started_at"],
            "turns": r["turns"],
            "outcome": final_outcome(r["outcome"], r["ended_at"], now),
            "reason": (r["outcome_reason"] or "").replace("_", " "),
            "rating": {1: "👍", -1: "👎"}.get(r["rating"], ""),
            "cost": f"{r['cost_usd_micros'] / 10_000:.2f}¢",
        }
        for r in conn.execute(
            "SELECT * FROM conversations ORDER BY started_at DESC LIMIT ?", (limit,)
        )
    ]
