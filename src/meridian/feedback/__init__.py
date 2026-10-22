"""The feedback loop (D16): auto-flags, staff review, add-as-test, handoff resolution codes.

Flags are one per (conversation, kind). Only a reviewed flag can become a test case. The web
app stores the case in the DB (the container's disk is wiped on deploy and holds no repo);
`evals.conversations.import_feedback` writes it locally into the eval dev area only, never the
frozen held-out or final cases (D4.6, D16.6, D16.8).
"""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from meridian import clock
from meridian.agent.handoff import Reason
from meridian.agent.state import Turn
from meridian.config import ROOT
from meridian.monitoring.metrics import (
    EMAIL,
    PHONE,
    final_outcome,
    mask_name,
    mask_phone,
    mask_text,
)

KINDS = (  # D16.2, in its order
    "thumbs_down",
    "answer_check_failed",
    "no_supporting_source",
    "abandoned",
    "error",
    "suspected_injection",
)
TAGS = {  # D16.3
    "wrong_answer": "Wrong answer",
    "search_missed": "Search missed the doc",
    "wrong_worker": "Wrong worker",
    "should_have_handled": "Should have handled",
    "should_not_have_handled": "Shouldn't have handled",
    "kb_missing": "KB is missing this",
    "actually_fine": "Actually fine",
}
RESOLUTIONS = {  # D16.9: the first two are handoffs the agent could have handled
    "just_needed_an_answer": "Just needed an answer (e.g. the warranty date)",
    "simple_booking_change": "Simple booking or visit change",
    "real_dispute": "Real billing dispute or complaint",
    "needed_an_exception": "Needed a waiver, refund or exception",
    "safety_or_urgent": "Safety or urgent dispatch",
    "could_not_reach": "Couldn't reach the caller",
    "other": "Other",
}
WRITE_TOOLS = ("book_slot", "reschedule_visit", "cancel_visit")
# What the eval runner needs from a feedback case (evals/conversations/run.py checks these).
CASE_KEYS = (
    "id", "split", "bucket", "source", "reviewed_by", "goal", "opening", "caller_notes", "kind",
    "facts_required", "facts_forbidden", "db_facts", "customer", "expected_handoff",
    "should_handle",
)  # fmt: skip
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
DEV_DIR = ROOT / "evals/conversations/dev"
FROZEN_CASES = ROOT / "evals/conversations/cases"  # held-out and final live here: never written
# D20.6: no detector, just phrases worth a human look (false alarms only cost a review).
_INJECTION = re.compile(
    r"ignore (?:all |any |your |the |my |previous |prior |above )*(?:rules|instructions|prompt)"
    r"|disregard (?:all |your |the |previous |prior )*(?:rules|instructions)"
    r"|system prompt|developer mode|jailbreak|you are now|pretend (?:you are|to be)",
    re.IGNORECASE,
)


def flag(conn: sqlite3.Connection, conversation_id: str, kind: str) -> None:
    """Records a flag (inside the caller's transaction); a repeat of the same kind is a no-op."""
    assert kind in KINDS
    conn.execute(
        "INSERT OR IGNORE INTO flags (conversation_id, kind, created_at) VALUES (?, ?, ?)",
        (conversation_id, kind, clock.ops_now().isoformat()),
    )


def flag_turn(conn: sqlite3.Connection, conversation_id: str, user_text: str, turn: Turn) -> None:
    """The per-turn triggers of D16.2. A handoff because the AI was down counts as an error."""
    reason = turn.ticket.reason if turn.ticket else None
    if turn.check_failures:
        flag(conn, conversation_id, "answer_check_failed")
    if reason is Reason.NO_SOURCE:
        flag(conn, conversation_id, "no_supporting_source")
    if turn.route == "error" or reason is Reason.MODEL_DOWN:
        flag(conn, conversation_id, "error")
    if _INJECTION.search(user_text):
        flag(conn, conversation_id, "suspected_injection")


def flag_abandoned(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """Abandoned is decided at read time (D15.2), so staff pages sweep for it before listing."""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "SELECT id, outcome, ended_at FROM conversations c "
        "WHERE (outcome IS NULL OR outcome = 'in_progress') AND NOT EXISTS "
        "(SELECT 1 FROM flags f WHERE f.conversation_id = c.id AND f.kind = 'abandoned')"
    ).fetchall()
    with conn:
        for r in rows:
            if final_outcome(r["outcome"], r["ended_at"], now) == "abandoned":
                flag(conn, r["id"], "abandoned")


def review_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Flags for the review page, unreviewed first. Caller details masked as in the queues."""
    rows = conn.execute(
        "SELECT f.*, c.customer_id, cu.name, cu.phone, "
        "(SELECT e.text FROM turn_events e WHERE e.conversation_id = f.conversation_id "
        " AND e.role = 'user' ORDER BY e.seq LIMIT 1) AS first_message, "
        "(SELECT group_concat(e.tool_calls) FROM turn_events e "
        " WHERE e.conversation_id = f.conversation_id) AS calls "
        "FROM flags f JOIN conversations c ON c.id = f.conversation_id "
        "LEFT JOIN customers cu ON cu.customer_id = c.customer_id "
        "ORDER BY f.reviewed_by IS NOT NULL, f.id DESC"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "kind": r["kind"].replace("_", " "),
            "created_at": r["created_at"],
            "caller": f"{mask_name(r['name'])} {mask_phone(r['phone'])}"
            if r["customer_id"]
            else "not verified",
            "first_message": mask_text(r["first_message"])[:300],
            "tag": r["review_tag"],
            "correct_answer": r["correct_answer"] or "",
            "reviewed_by": r["reviewed_by"] or "",
            "added_as_test": bool(r["added_as_test"]),
            "wrote": any(t in (r["calls"] or "") for t in WRITE_TOOLS),
        }
        for r in rows
    ]


def review(
    conn: sqlite3.Connection, flag_id: int, tag: str, correct_answer: str, reviewed_by: str
) -> None:
    reviewed_by = reviewed_by.strip()[:80]
    if tag not in TAGS or not reviewed_by:
        raise ValueError("pick a tag and say who reviewed it")
    with conn:
        cur = conn.execute(
            "UPDATE flags SET review_tag = ?, correct_answer = ?, reviewed_by = ? WHERE id = ?",
            (tag, correct_answer.strip()[:2000] or None, reviewed_by, flag_id),
        )
    if cur.rowcount != 1:
        raise LookupError("no such flag")


def is_feedback_case(case: object) -> bool:
    """A reviewed feedback case with every key the eval runner reads."""
    if not isinstance(case, dict):
        return False
    c = cast(dict[str, Any], case)
    return (
        all(k in c for k in CASE_KEYS)
        and c["source"] == "feedback"
        and c["split"] == "dev"
        and bool(c["reviewed_by"])
        and isinstance(c["customer"], dict)
    )


def case_file_name(case: dict[str, Any]) -> str:
    """feedback-<conversation>-<kind>.json: stable across delete-logs, unlike flag ids."""
    cid, kind = str(case.get("conversation_id", "")), str(case.get("flag_kind", ""))
    if not _SAFE_ID.fullmatch(cid) or kind not in KINDS:
        raise ValueError("bad conversation id or flag kind in the case")
    return f"feedback-{cid}-{kind}.json"


def dev_case_path(dest: Path, case: dict[str, Any]) -> Path:
    """Where a feedback case goes. Anything but a `dev` folder outside the frozen cases is
    refused, so the held-out and final splits can't be written (D4.6, D16.8)."""
    d = dest.resolve()
    if d.name != "dev" or d.is_relative_to(FROZEN_CASES.resolve()):
        raise PermissionError("feedback cases go to the eval dev area only")
    return d / case_file_name(case)


def write_dev_case(case: dict[str, Any], dest: Path | None = None) -> Path:
    """Local export only (never the web app). Refuses an existing file or an invalid case."""
    if not is_feedback_case(case):
        raise ValueError("not a reviewed feedback case")
    path = dev_case_path(dest or DEV_DIR, case)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as out:
        out.write(json.dumps(case, ensure_ascii=False, indent=1) + "\n")
    return path


def _swap_contacts(text: str, customer: dict[str, Any] | None) -> str:
    """Like the case builder: the caller's typed phone and email become the case customer's,
    so verification still works; with no verified customer they're masked."""
    if customer is None:
        return mask_text(text)
    text = EMAIL.sub(str(customer["email"]), text)
    return PHONE.sub(str(customer["phone"]), text)


def add_as_test(conn: sqlite3.Connection, flag_id: int, expected: str) -> dict[str, Any]:
    """Builds a dev-set conversation case from a reviewed flag (D16.4) and stores it on the
    flag. `expected` is "handle" or the handoff reason the call should get.
    shortcut: "handle" cases are question-style (the DB must stay unchanged), so a
    conversation that booked, moved or cancelled is refused; a booking case needs a
    hand-written expected DB change. Upgrade when a booking flag needs one."""
    f = conn.execute("SELECT * FROM flags WHERE id = ?", (flag_id,)).fetchone()
    if f is None:
        raise LookupError("no such flag")
    if not f["reviewed_by"] or not f["review_tag"]:
        raise ValueError("only reviewed flags become test cases (D16.6)")
    if f["added_as_test"]:
        raise ValueError("already added as a test")
    if expected != "handle" and expected not in {r.value for r in Reason}:
        raise ValueError("unknown expected outcome")
    events = conn.execute(
        "SELECT role, text, tool_calls FROM turn_events WHERE conversation_id = ? ORDER BY seq",
        (f["conversation_id"],),
    ).fetchall()
    if expected == "handle" and any(
        t in (e["tool_calls"] or "") for e in events for t in WRITE_TOOLS
    ):
        raise ValueError(
            "this conversation booked, moved or cancelled a visit; a 'handled' case can only "
            "check questions (the DB must stay unchanged); add it as a handoff or by hand"
        )
    cu = conn.execute(
        "SELECT cu.* FROM conversations c JOIN customers cu ON cu.customer_id = c.customer_id "
        "WHERE c.id = ?",
        (f["conversation_id"],),
    ).fetchone()
    customer = (
        {
            "customer_id": cu["customer_id"],
            "name": cu["name"],
            "phone": cu["phone"],
            "email": cu["email"],
            "address": f"{cu['service_address']}, {cu['city']}",
            "tier": cu["membership_tier"],
            "appointment_id": None,
            "visit": None,
        }
        if cu
        else None
    )
    said = [_swap_contacts(e["text"], customer) for e in events if e["role"] == "user"]
    if not said:
        raise ValueError("the conversation has no caller messages")
    handle = expected == "handle"
    answer = f["correct_answer"]
    case: dict[str, Any] = {
        "id": f"FLAG-{f['conversation_id']}-{f['kind']}",
        "split": "dev",
        "bucket": "feedback",
        "source": "feedback",
        "flag_kind": f["kind"],
        "review_tag": f["review_tag"],
        "reviewed_by": f["reviewed_by"],
        "conversation_id": f["conversation_id"],
        "goal": "Get the same help as in a logged conversation; your own words are below.",
        "opening": said[0],
        "caller_notes": "Say these follow-ups in order when they fit: "
        + (" / ".join(said[1:])[:1500] or "none"),
        "kind": "kb_question" if handle else "handoff",
        "service_type": None,
        "also_books": False,
        "accepts_fee": False,
        "facts_required": [answer] if handle and answer else [],
        "facts_forbidden": [],
        "db_facts": [],
        "customer": customer
        or {
            "customer_id": "not given",
            "name": "not given",
            "phone": "not given",
            "email": "not given",
            "address": "not given",
            "tier": "unknown",
            "appointment_id": None,
            "visit": None,
        },
        "expected_handoff": None if handle else expected,
        "should_handle": handle,
        "reviewer_note": answer or "",
    }
    case_file_name(case)  # refuse now, not at export, if the name can't be made
    with conn:  # the case and the "added" mark land together
        conn.execute(
            "UPDATE flags SET added_as_test = 1, test_case = ? WHERE id = ?",
            (json.dumps(case, ensure_ascii=False), flag_id),
        )
    return case


def resolve_ticket(conn: sqlite3.Connection, ticket_id: int, code: str) -> None:
    """Staff mark a handoff done with how it was resolved (D16.9)."""
    if code not in RESOLUTIONS:
        raise ValueError("pick a resolution code")
    with conn:
        cur = conn.execute(
            "UPDATE handoff_tickets SET status = 'closed', resolution_code = ? "
            "WHERE id = ? AND status = 'open'",
            (code, ticket_id),
        )
    if cur.rowcount != 1:
        raise LookupError("no such open ticket")


def resolution_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT resolution_code, count(*) FROM handoff_tickets "
        "WHERE resolution_code IS NOT NULL GROUP BY resolution_code ORDER BY count(*) DESC"
    ).fetchall()
    return [(RESOLUTIONS.get(r[0], str(r[0])), int(r[1])) for r in rows]
