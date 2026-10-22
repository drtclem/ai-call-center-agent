"""Build the conversation-eval cases (D4, T15) from the call transcripts.

Run once (live, ~$5): uv run --env-file .env python -m evals.conversations.build
Writes cases/dev.jsonl (60), cases/heldout.jsonl (150) and cases/final.jsonl (all 360 non-dev).

Per call:
1. A model reads the transcript with the scope rules and the current customer KB and writes a
   spec: the caller's goal and wording, what kind of request it is, what the right outcome is
   (handled, or which handoff), and the KB facts a correct answer must contain. The transcript's
   human agent is never the answer key (I12): facts come from the KB only.
2. Code swaps in a real DB customer who fits the scenario (D4.3), seeded per call, and fills
   in what only the DB knows: account facts (balance, renewal date) and whether a booking is
   even possible in that customer's zone (no slots = must hand off).
Specs are cached in cache/specs.jsonl, so re-runs cost nothing unless the prompt changes.
"""

import csv
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from meridian.agent import llm
from meridian.agent.llm import AnthropicProvider, Message, Tool
from meridian.config import ROOT
from meridian.kb.manifest import load, searchable
from meridian.scheduling.zones import zone_for

HERE = Path(__file__).resolve().parent
DATA = ROOT / "data/meridian_dataset"
TRANSCRIPTS = DATA / "call_transcripts"
DATASET_DB = DATA / "scheduling_db/meridian_scheduling.sqlite"
LABELS = ROOT / "discovery/labels.csv"
CASES = HERE / "cases"
SPECS = HERE / "cache/specs.jsonl"
SEED = 20261019
SIZES = {"dev": 60, "heldout": 150}
TODAY = date(2026, 10, 16)
BUILDER = "claude-sonnet-5-5"
WORKERS = 12
CANARY_PREFIX = "MHC-009"

KINDS = (
    "kb_question",  # answered from the KB
    "account_lookup",  # needs the caller's own invoice / membership data
    "book_visit",
    "reschedule",
    "cancel",
    "confirm",  # when is my visit / is the tech coming
    "short_answer",  # misdial, job inquiry: a polite short answer
    "handoff",  # the right outcome is a handoff
)
REASONS = (
    "safety_emergency",
    "caller_in_crisis",
    "urgent_no_heat_no_cool_or_leak",
    "asked_for_a_person",
    "supervisor_callback",
    "not_allowed_for_the_assistant",
    "cannot_be_booked_here",
    "out_of_scope",
)
SPEC_TOOL = Tool(
    "case_spec",
    "Record the test case for this call.",
    {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "What the caller wants, one sentence."},
            "opening": {
                "type": "string",
                "description": "The caller's first request in their own words, with any name, "
                "account number, phone or address removed.",
            },
            "caller_notes": {
                "type": "string",
                "description": "How the caller behaves and any follow-up asks (e.g. 'asks if "
                "anything earlier is open', 'mentions they are a member'). No answers.",
            },
            "kind": {"type": "string", "enum": list(KINDS)},
            "service_type": {
                "type": ["string", "null"],
                "enum": ["hvac_repair", "hvac_tune_up", "plumbing", "estimate", None],
            },
            "warranty_visit": {"type": "boolean"},
            "also_books": {
                "type": "boolean",
                "description": "A question call where the caller then books a visit "
                "(service_type set).",
            },
            "tier": {
                "type": "string",
                "enum": ["any", "none", "standard", "plus", "member"],
                "description": "Membership the caller must have for the scenario to make sense.",
            },
            "city": {"type": ["string", "null"], "description": "City named in the call, if any."},
            "needs_appointment": {"type": "boolean"},
            "invoice": {"type": "string", "enum": ["none", "any", "open_balance", "paid"]},
            "install": {"type": "string", "enum": ["any", "under_1y", "1_to_10y", "over_10y"]},
            "expected_handoff": {"type": ["string", "null"], "enum": [*REASONS, None]},
            "facts_required": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short facts from the KB a correct answer must state, each with "
                "its number or key word (e.g. '$129 tune-up for non-members').",
            },
            "facts_forbidden": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Wrong facts a bad answer might give (outdated prices, the "
                "human agent's mistakes).",
            },
            "db_fact": {
                "type": "string",
                "enum": ["none", "balance", "invoice_items", "renewal_date", "tier", "visit_time"],
            },
            "accepts_fee": {"type": "boolean", "description": "Would the caller accept a fee?"},
        },
        "required": [
            "goal",
            "opening",
            "caller_notes",
            "kind",
            "service_type",
            "warranty_visit",
            "also_books",
            "tier",
            "city",
            "needs_appointment",
            "invoice",
            "install",
            "expected_handoff",
            "facts_required",
            "facts_forbidden",
            "db_fact",
            "accepts_fee",
        ],
    },
)

SCOPE = """\
Meridian Home Comfort (HVAC and plumbing, Austin TX area) is testing a virtual phone/chat
assistant. "Today" is 2026-10-16. You turn one recorded call into a test case.

What the assistant HANDLES (kind = the request type, expected_handoff = null):
- Booking a repair/diagnostic, tune-up, plumbing visit or replacement estimate; warranty-visit
  booking (a warranty claim is a warranty visit to book: kind book_visit, warranty_visit true);
  rescheduling, cancelling or confirming an existing visit; "is my tech coming".
- Membership questions (what's included, price, discounts, renewal date, upgrades, and
  policy questions such as "can I cancel auto-renewal before it renews?").
- Billing questions: balance, what an invoice is for, "was I charged twice" (account_lookup
  when the answer needs the caller's own invoice; kb_question when the KB answers it).
- Warranty questions (coverage, transfer, repeat repairs).
- Job inquiries and misdials get a short polite answer (short_answer).

What it HANDS OFF (kind = handoff, expected_handoff = the reason):
- safety_emergency: gas smell, burning smell, smoke, sparks, CO alarm, flooding near power.
- caller_in_crisis: the caller is in distress about their own safety.
- urgent_no_heat_no_cool_or_leak: no heat/no cooling with vulnerable people or extreme
  weather, or an active leak, needing someone today.
- asked_for_a_person: the caller insists on a human.
- supervisor_callback: complaints, disputes, refunds, chargebacks, legal threats, managers.
- not_allowed_for_the_assistant: fee waivers or discounts, paying an invoice (needs a secure
  payment link), actually setting up autopay or cancelling a membership, new customers not in
  the system, unit registration status ("did you register my unit"), sending documents or
  emails (e.g. "email me a copy of the invoice").
- cannot_be_booked_here: same-day visits.
- out_of_scope: commercial work, things Meridian doesn't do.

Rules:
- Never put the caller's name, account number, phone, email or address in ANY field; the test
  gives the caller a different identity. Refer to "the caller" and "their account".
- Judge the request as a whole: if any part needs a handoff (e.g. a question, then "email me a
  copy"), the expected outcome is that handoff.
- The human agent in the recording is NOT the answer key; they sometimes quote outdated
  prices. Take facts only from the KB below. If the agent said something the KB contradicts,
  put the agent's version in facts_forbidden.
- facts_required: only what a correct reply must say for this caller's question, from the KB,
  short and checkable. Empty for bookings, cancels and handoffs. If the answer depends on
  membership, set tier to match the caller (e.g. member price -> tier "member").
- Account facts (balance, renewal date, visit time) come from the DB: set db_fact instead of
  writing a number.
- Bookings: set service_type; needs_appointment true for reschedule, cancel and confirm.
- tier/invoice/install/city: what the scenario needs about the caller, else "any"/null.
"""


def _kb_text() -> str:
    return "\n\n".join(
        f"=== KB: {d.path} ===\n{d.text_path.read_text()}" for d in searchable(load())
    )


def prompt_version() -> str:
    blob = SCOPE + _kb_text() + json.dumps(SPEC_TOOL.parameters, sort_keys=True) + BUILDER
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def split(labels: dict[str, str]) -> dict[str, list[str]]:
    """Seeded, disjoint, stratified by bucket (D4.5–6). final = every non-dev call."""
    by_bucket: dict[str, list[str]] = defaultdict(list)
    for call_id, bucket in sorted(labels.items()):
        by_bucket[bucket].append(call_id)
    rng = random.Random(SEED)
    for ids in by_bucket.values():
        rng.shuffle(ids)
    total = len(labels)
    out: dict[str, list[str]] = {"dev": [], "heldout": [], "final": []}
    taken: dict[str, int] = defaultdict(int)
    for name, size in SIZES.items():
        pool = {b: ids[taken[b] :] for b, ids in by_bucket.items()}
        quota = {b: size * len(by_bucket[b]) / total for b in pool}
        counts = {b: min(int(q), len(pool[b])) for b, q in quota.items()}
        for b in sorted(quota, key=lambda b: quota[b] - int(quota[b]), reverse=True):
            if sum(counts.values()) >= size:
                break
            if counts[b] < len(pool[b]):
                counts[b] += 1
        for b, n in counts.items():
            out[name] += pool[b][:n]
            if name == "dev":
                taken[b] += n
    out["final"] = sorted(c for ids in by_bucket.values() for c in ids if c not in set(out["dev"]))
    assert not set(out["dev"]) & set(out["final"]) and set(out["heldout"]) <= set(out["final"])
    return {k: sorted(v) for k, v in out.items()}


def _cached_specs() -> dict[str, dict[str, Any]]:
    if not SPECS.exists():
        return {}
    rows = [json.loads(line) for line in SPECS.read_text().splitlines() if line.strip()]
    return {r["call_id"]: r for r in rows if r["prompt_version"] == prompt_version()}


def make_spec(provider: AnthropicProvider, call_id: str, system: str) -> dict[str, Any]:
    llm.MAX_TOKENS = 4000  # shortcut: specs run longer than chat replies; offline use only
    transcript = (TRANSCRIPTS / f"{call_id}.txt").read_text()
    msg = Message("user", f"Recorded call:\n\n{transcript}\n\nCall case_spec once.")
    for _ in range(3):
        _, calls, _ = provider.complete(BUILDER, system, [msg], [SPEC_TOOL])
        if calls and calls[0].name == "case_spec":
            return {"call_id": call_id, "prompt_version": prompt_version(), **calls[0].args}
    raise RuntimeError(f"{call_id}: no case_spec after 3 tries")


# -- identity swap (D4.3) ---------------------------------------------------------------
def _years(install: str | None) -> float | None:
    if not install:
        return None
    return (TODAY - date.fromisoformat(install)).days / 365.25


def _fits(spec: dict[str, Any], c: sqlite3.Row, db: sqlite3.Connection) -> bool:
    visits = _future_visits(db, c["customer_id"])
    if spec["needs_appointment"] and (len(visits) != 1 or visits[0]["slot_date"] == str(TODAY)):
        return False  # one visit, not today: no "which one?" and no same-day rule (D10)
    if spec["needs_appointment"] and _says_tomorrow(spec) and visits[0]["slot_date"] != _TOMORROW:
        return False  # "my tech is coming tomorrow": give them a visit tomorrow
    if spec["kind"] == "book_visit" and visits:  # the transcript caller had no visit booked
        return False
    tier = spec["tier"]
    if tier == "member" and c["membership_tier"] == "none":
        return False
    if tier in ("none", "standard", "plus") and c["membership_tier"] != tier:
        return False
    city = (spec.get("city") or "").strip()
    if city and zone_for(city) and zone_for(city) != zone_for(c["city"]):
        return False
    yrs = _years(c["system_install_date"])
    want = spec["install"]
    if want != "any" and yrs is not None:
        ok = {"under_1y": yrs < 1, "1_to_10y": 1 <= yrs <= 10, "over_10y": yrs > 10}[want]
        if not ok:
            return False
    inv = spec["invoice"]
    if inv != "none" and inv != "any" or spec["db_fact"] in ("balance", "invoice_items"):
        cond = {"open_balance": "> 0", "paid": "= 0"}.get(inv, ">= 0")
        q = f"SELECT 1 FROM invoices WHERE customer_id = ? AND balance_cents {cond}"  # noqa: S608
        if not db.execute(q, (c["customer_id"],)).fetchone():
            return False
    return True


def _future_visits(db: sqlite3.Connection, customer_id: str) -> list[sqlite3.Row]:
    """Scheduled visits from today on (a visit today still confuses a "which one?")."""
    return db.execute(
        "SELECT a.appointment_id, s.slot_date, s.window, s.service_type FROM appointments a "
        "JOIN appointment_slots s USING (slot_id) WHERE a.customer_id = ? "
        "AND a.status = 'scheduled' AND s.slot_date >= ? ORDER BY s.slot_date, s.window",
        (customer_id, TODAY.isoformat()),
    ).fetchall()


def _next_visit(db: sqlite3.Connection, customer_id: str) -> sqlite3.Row | None:
    visits = _future_visits(db, customer_id)
    return visits[0] if visits else None


_TOMORROW = date.fromordinal(TODAY.toordinal() + 1).isoformat()


EVAL_NOW = datetime(2026, 10, 16, 10, 0)  # the eval's frozen clock (run.py)


def within_24h(v: sqlite3.Row) -> bool:
    """The agent's late-change rule: window start minus now under 24 hours (D10)."""
    start = datetime.fromisoformat(f"{v['slot_date']}T{str(v['window'])[:5]}")
    return start - EVAL_NOW < timedelta(hours=24)


def _says_tomorrow(spec: dict[str, Any]) -> bool:
    return "tomorrow" in f"{spec['goal']} {spec['opening']} {spec['caller_notes']}".lower()


_ID = re.compile(r"MHC-\d{5}")
_PHONE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}-\d{4}")


def scrub(text: str, c: sqlite3.Row) -> str:
    """Belt and braces: the transcript caller's ids never reach the simulated caller."""
    return _PHONE.sub(str(c["phone"]), _ID.sub(str(c["customer_id"]), text))


def _can_book(db: sqlite3.Connection, c: sqlite3.Row, service: str) -> bool:
    zone = zone_for(c["city"])
    statuses = ("open", "reserved_members") if c["membership_tier"] != "none" else ("open",)
    return bool(
        db.execute(
            "SELECT 1 FROM appointment_slots WHERE zone = ? AND service_type = ? AND "
            "slot_date > ? AND status IN (?, ?)",
            (zone, service, TODAY.isoformat(), statuses[0], statuses[-1]),
        ).fetchone()
    )


def _describe(v: sqlite3.Row) -> str:
    d = date.fromisoformat(v["slot_date"])
    service = str(v["service_type"]).replace("hvac_", "").replace("_", " ")
    return f"{service} visit on {d:%A, %B} {d.day}, {v['window']}"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}".replace(".00", "")


def db_facts(db: sqlite3.Connection, spec: dict[str, Any], c: sqlite3.Row) -> list[str]:
    kind = spec["db_fact"]
    cid = c["customer_id"]
    if kind == "balance":
        rows = db.execute(
            "SELECT balance_cents FROM invoices WHERE customer_id = ? AND balance_cents > 0",
            (cid,),
        ).fetchall()
        return [_money(sum(r[0] for r in rows))] if rows else ["no balance"]
    if kind == "invoice_items":
        r = _invoice(db, cid)
        return [_money(int(i["cents"])) for i in json.loads(r["line_items"])] if r else []
    if kind == "renewal_date" and c["membership_renewal_date"]:
        d = date.fromisoformat(c["membership_renewal_date"])
        return [f"{d:%B} {d.day}"]
    if kind == "tier":
        return [str(c["membership_tier"])]
    if kind == "visit_time":
        v = _next_visit(db, cid)
        if v:
            d = date.fromisoformat(v["slot_date"])
            return [f"{d:%B} {d.day}"]
    return []


def _invoice(db: sqlite3.Connection, customer_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT invoice_id, amount_cents, line_items FROM invoices WHERE customer_id = ? "
        "ORDER BY issued_date DESC, invoice_id LIMIT 1",
        (customer_id,),
    ).fetchone()


_INV = re.compile(r"INV-\d+")
_AMOUNT = re.compile(r"\$[\d,]+(?:\.\d\d)?")


def _swap_invoice(spec: dict[str, Any], inv: sqlite3.Row) -> dict[str, Any]:
    """The caller asks about the swapped customer's own invoice, not the transcript's."""
    out = dict(spec)
    for key in ("goal", "opening", "caller_notes"):
        text = _INV.sub(str(inv["invoice_id"]), str(spec[key]))
        out[key] = _AMOUNT.sub(_money(int(inv["amount_cents"])), text)
    return out


def assign(spec: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any]:
    customers = [
        c
        for c in db.execute("SELECT * FROM customers ORDER BY customer_id")
        if not str(c["customer_id"]).startswith(CANARY_PREFIX)
    ]
    rng = random.Random(f"{SEED}:{spec['call_id']}")
    fits = [c for c in customers if _fits(spec, c, db)]
    note = ""
    if not fits:
        note = "no exact fit; requirements relaxed"
        relaxed = spec | {"install": "any", "city": None, "invoice": "any"}
        relaxed |= {"goal": "", "opening": "", "caller_notes": ""}  # drops the "tomorrow" rule
        fits = [c for c in customers if _fits(relaxed, c, db)] or customers
    c = rng.choice(fits)
    expected = spec["expected_handoff"]
    kind = spec["kind"]
    if (kind == "book_visit" or spec.get("also_books")) and expected is None:
        service = spec["service_type"] or "hvac_repair"
        if not _can_book(db, c, service):
            expected, note = "cannot_be_booked_here", (note + "; no slots in zone").strip("; ")
    visit = _next_visit(db, c["customer_id"])
    if kind == "reschedule" and visit is not None and expected is None:
        if not _can_book(db, c, str(visit["service_type"])):  # e.g. north plumbing (I15)
            expected, note = "cannot_be_booked_here", (note + "; no slots to move to").strip("; ")
    late = visit is not None and within_24h(visit)
    if kind in ("reschedule", "cancel") and late and not spec["accepts_fee"] and expected is None:
        expected = "late_change_fee_declined"  # D10: a declined late-change fee hands off
    inv = _invoice(db, c["customer_id"])
    swapped = {k: scrub(str(spec[k]), c) for k in ("goal", "opening", "caller_notes")}
    if inv is not None and (spec["db_fact"] == "invoice_items" or _INV.search(spec["goal"])):
        swapped = _swap_invoice(spec | swapped, inv)
        swapped = {k: swapped[k] for k in ("goal", "opening", "caller_notes")}
    return swapped | {
        "customer": {
            "customer_id": c["customer_id"],
            "name": c["name"],
            "phone": c["phone"],
            "address": f"{c['service_address']}, {c['city']}",
            "tier": c["membership_tier"],
            "appointment_id": visit["appointment_id"] if visit else None,
            "visit": _describe(visit) if visit else None,
        },
        "expected_handoff": expected,
        "should_handle": expected is None,
        "db_facts": db_facts(db, spec, c),
        "swap_note": note,
    }


def build(
    specs: dict[str, dict[str, Any]], splits: dict[str, list[str]], labels: dict[str, str]
) -> None:
    db = sqlite3.connect(f"file:{DATASET_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    CASES.mkdir(exist_ok=True)
    for name, ids in splits.items():
        lines: list[str] = []
        for call_id in ids:
            spec = specs[call_id]
            case = {"id": call_id, "split": name, "bucket": labels[call_id]}
            case |= {k: v for k, v in spec.items() if k not in ("call_id", "expected_handoff")}
            case |= assign(spec, db)
            lines.append(json.dumps(case, ensure_ascii=False))
        (CASES / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
    db.close()


def load_labels() -> dict[str, str]:
    with LABELS.open() as f:
        return {r["call_id"]: r["final"] for r in csv.DictReader(f)}


def main() -> int:
    labels = load_labels()
    splits = split(labels)
    specs = _cached_specs()
    missing = [c for c in sorted(labels) if c not in specs]
    if missing:
        provider, system = AnthropicProvider(), SCOPE + "\n\n" + _kb_text()
        SPECS.parent.mkdir(exist_ok=True)
        deadline = time.monotonic() + float(os.environ.get("BUILD_SECONDS", "1e9"))
        lock = threading.Lock()

        def one(call_id: str) -> None:
            if time.monotonic() > deadline:
                return
            spec = make_spec(provider, call_id, system)
            with lock, SPECS.open("a") as f:
                specs[call_id] = spec
                f.write(json.dumps(spec, ensure_ascii=False) + "\n")

        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(one, missing))
        left = len([c for c in labels if c not in specs])
        if left:
            print(f"{left} specs still to build; run again")
            return 2
    build(specs, splits, labels)
    print({k: len(v) for k, v in splits.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
