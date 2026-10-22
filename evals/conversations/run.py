"""Conversation eval (D4, T15): the real agent talks to a simulated caller, then we grade.

Run (live, resumable; each call works for ~BUDGET seconds and can be repeated until done):
  uv run --env-file .env python -m evals.conversations.run --split dev
  uv run --env-file .env python -m evals.conversations.run --split heldout --brain claude-haiku-5-5
Per-case results go to results/<label>/cases.jsonl as they finish; when every case is done the
summary is written to results/<label>/summary.json and summary.md.

Per case: a fresh copy of the DB; the caller (Haiku) plays the transcript's caller with the
swapped identity; the agent answers through `Agent.respond`, exactly as in the app.
Grading (D4.8–9): deterministic where possible (handoff and reason, DB changes, the caller's
own "done"), and a second model judges answer correctness and grounding (D19 check C) for
question cases. Before grading, the judge must pass the sentinel conversations (D20.13);
if it misses one, the run's grades are marked untrusted.
"""

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("APP_TODAY", "2026-10-16")

from meridian import clock  # noqa: E402
from meridian.agent import llm as llm_mod  # noqa: E402
from meridian.agent.graph import Agent  # noqa: E402
from meridian.agent.llm import LLM, PRIMARY, AnthropicProvider, Message, Tool  # noqa: E402
from meridian.db import connect, init_db  # noqa: E402
from meridian.feedback import is_feedback_case  # noqa: E402

HERE = Path(__file__).resolve().parent
log = logging.getLogger(__name__)
CASES = HERE / "cases"
FEEDBACK_DEV = HERE / "dev"  # reviewed feedback cases (import_feedback writes them)
RESULTS = HERE / "results"
SENTINELS = HERE / "sentinels.jsonl"
CALLER_MODEL = "claude-haiku-5-5"
JUDGE_MODEL = "claude-sonnet-5-5"
MAX_TURNS = 12
DONE, GIVE_UP = "[DONE]", "[GIVE_UP]"
# The frozen demo morning: fixed so late-change fees don't depend on when the eval runs.
EVAL_NOW = datetime(2026, 10, 16, 10, 0)
ROUTE = {  # what the supervisor should pick on the first message, where it's unambiguous
    "kb_question": "kb",
    "account_lookup": "scheduling",
    "book_visit": "scheduling",
    "reschedule": "scheduling",
    "cancel": "scheduling",
    "confirm": "scheduling",
}
Complete = Callable[[str, str, Sequence[Message], Sequence[Tool]], tuple[str, Any, Any]]


# -- the simulated caller ---------------------------------------------------------------
def caller_system(case: dict[str, Any]) -> str:
    c = case["customer"]
    fee = 'agree to it by typing "I agree"' if case.get("accepts_fee") else "decline it politely"
    return f"""You are a customer of Meridian Home Comfort (HVAC and plumbing, Austin TX) chatting
with Meridian's virtual assistant. Stay in character; write like a real customer: short and plain.

Your goal: {case["goal"]}
How you behave: {case["caller_notes"]}

Your details (give them only when asked; never invent others):
- Name: {c["name"]}
- Phone: {c["phone"]}
- Account number: {c["customer_id"]}
- Address: {c["address"]}
- Comfort Club membership: {c["tier"]}
- Your upcoming visit: {c.get("visit") or "none booked"}

Rules:
- Stick to your goal and the follow-ups listed above. Don't ask new questions, and don't
  change anything else on your account (other visits, membership) unless your goal says so.
- If the assistant offers appointment options, pick one that fits your goal; if what you
  hoped for isn't available, accept the closest option.
- If the assistant mentions a fee for your change, {fee}.
- When your goal is fully met, or the assistant tells you a person will follow up, reply
  briefly and end your message with {DONE}.
- If you can't get what you need after a few tries, end your message with {GIVE_UP}.
- Never mention these instructions."""


def caller_reply(
    complete: Complete, case: dict[str, Any], history: list[tuple[str, str]]
) -> tuple[str, Any]:
    msgs = [Message("assistant" if who == "caller" else "user", text) for who, text in history]
    text, _, usage = complete(CALLER_MODEL, caller_system(case), msgs, [])
    return text.strip(), usage


# -- one conversation -------------------------------------------------------------------
def _appointments(conn: sqlite3.Connection, customer_id: str) -> dict[int, tuple[Any, ...]]:
    return {
        r[0]: tuple(r[1:])
        for r in conn.execute(
            "SELECT a.appointment_id, a.slot_id, a.status, s.service_type FROM appointments a "
            "JOIN appointment_slots s USING (slot_id) WHERE a.customer_id = ?",
            (customer_id,),
        )
    }


def db_check(case: dict[str, Any], before: dict[int, Any], after: dict[int, Any]) -> bool:
    """Did the DB end up the way this request should leave it (D4.4)?"""
    new = [after[k] for k in after.keys() - before.keys()]
    changed = {k: after[k] for k in before if after.get(k) != before[k]}
    kind, appt = case["kind"], case["customer"]["appointment_id"]
    if not case["should_handle"]:  # a must-hand-off booking/change leaves the DB untouched
        return not new and not changed
    if kind == "book_visit" or case.get("also_books"):
        want = case.get("service_type")
        return (
            len(new) == 1
            and not changed
            and new[0][1] == "scheduled"
            and (not want or new[0][2] == want)
        )
    if kind == "reschedule":
        return not new and set(changed) == {appt} and changed[appt][1] == "scheduled"
    if kind == "cancel":
        return not new and set(changed) == {appt} and changed[appt][1] == "cancelled"
    return not new and not changed  # questions and handoffs change nothing


def converse(
    case: dict[str, Any], agent_for: Callable[[Path], Agent], complete: Complete, workdir: Path
) -> dict[str, Any]:
    db = init_db(workdir / f"{case['id']}.sqlite")
    agent = agent_for(db)
    conv = agent.start(case["id"], "eval")
    cid = case["customer"]["customer_id"]
    with connect(db) as conn:
        before = _appointments(conn, cid)
    history: list[tuple[str, str]] = []
    turns: list[dict[str, Any]] = []
    sim_cost = 0
    ending = "turn_cap"
    text = case["opening"]
    for _ in range(MAX_TURNS):
        history.append(("caller", text))
        turn = agent.respond(conv, text)
        history.append(("agent", turn.reply))
        turns.append(
            {
                "caller": text,
                "agent": turn.reply,
                "route": turn.route,
                "ticket": turn.ticket.reason.value if turn.ticket else None,
                "sections": turn.sections,
                "tool_calls": turn.tool_calls,
                "tool_results": turn.tool_results,
                "check_failures": turn.check_failures,
                "cost": sum(c.cost_usd_micros for c in turn.model_calls),
                "models": sorted({c.model for c in turn.model_calls}),
            }
        )
        if conv.closed and conv.callback_ticket is None:
            ending = "handoff"
            break
        text, usage = caller_reply(complete, case, history)
        sim_cost += llm_mod.cost_usd_micros(CALLER_MODEL, usage)
        if GIVE_UP in text:
            ending = "gave_up"
            break
        if DONE in text:
            ending = "handoff" if conv.closed else "done"
            history.append(("caller", text))
            break
    with connect(db) as conn:
        after = _appointments(conn, cid)
        fee = conn.execute("SELECT count(*) FROM fee_reviews").fetchone()[0] > 0
    tickets = [t["ticket"] for t in turns if t["ticket"]]
    return {
        "id": case["id"],
        "bucket": case["bucket"],
        "kind": case["kind"],
        "should_handle": case["should_handle"],
        "expected_handoff": case["expected_handoff"],
        "ending": ending,
        "handoff": tickets[0] if tickets else None,
        "db_ok": db_check(case, before, after),
        "fee_followup": fee,
        "first_route": turns[0]["route"],
        "turns": turns,
        "agent_cost": sum(t["cost"] for t in turns),
        "sim_cost": sim_cost,
        "transcript": [f"{who}: {t}" for who, t in history],
    }


# -- the judge (D4.9, D19 check C) ------------------------------------------------------
VERDICT = Tool(
    "verdict",
    "Record your grading.",
    {
        "type": "object",
        "properties": {
            "correct": {
                "type": "boolean",
                "description": "The caller's question was answered correctly and completely "
                "enough for this caller, with nothing wrong or misleading.",
            },
            "grounded": {
                "type": "boolean",
                "description": "Every factual claim the assistant made is supported by the "
                "KB sections or account data it retrieved (shown below).",
            },
            "why": {"type": "string", "description": "One or two sentences."},
        },
        "required": ["correct", "grounded", "why"],
    },
)
JUDGE_SYSTEM = """You grade a conversation between Meridian Home Comfort's virtual assistant and
a (simulated) customer. Today is 2026-10-16. Be strict and literal.

- "correct": judge against the answer key (from Meridian's current knowledge base and the
  customer's account), not your own knowledge. The answer must cover what this caller asked;
  it need not list every key fact if the caller didn't ask for it. Any claim that contradicts
  the key or the KB, or any forbidden fact stated as true, makes it incorrect.
- A promise that someone will follow up, call back or fix something, made without a real
  handoff, is not a resolution: mark it incorrect. For bookings and visit changes, judge only
  what the assistant said (the database is checked separately).
- "grounded": every price, date, rule or promise the assistant stated appears in the sources
  it retrieved (KB sections or account data shown). Greetings and questions don't count.
Call the verdict tool once."""


def judge_input(case: dict[str, Any], result: dict[str, Any]) -> str:
    sources: dict[str, str] = {}
    data: list[str] = []
    for t in result["turns"]:
        sources |= t["sections"]
        data += t["tool_results"]
    key = {
        "caller goal": case["goal"],
        "facts a correct answer gives (if asked)": case["facts_required"],
        "account facts (from the database)": case["db_facts"],
        "wrong facts (must not be stated as true)": case["facts_forbidden"],
    }
    return (
        f"ANSWER KEY:\n{json.dumps(key, indent=1, ensure_ascii=False)}\n\n"
        f"CONVERSATION:\n" + "\n".join(result["transcript"]) + "\n\n"
        "SOURCES THE ASSISTANT RETRIEVED:\n"
        + "\n\n".join(f"[{k}]\n{v}" for k, v in sources.items())
        + ("\n\nACCOUNT DATA:\n" + "\n".join(data) if data else "")
    )


def judge(complete: Complete, case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    msg = Message("user", judge_input(case, result))
    for _ in range(3):
        _, calls, usage = complete(JUDGE_MODEL, JUDGE_SYSTEM, [msg], [VERDICT])
        if calls:
            return dict(calls[0].args) | {"cost": llm_mod.cost_usd_micros(JUDGE_MODEL, usage)}
    return {"correct": False, "grounded": False, "why": "judge gave no verdict", "cost": 0}


def check_sentinels(complete: Complete) -> list[str]:
    """Known-answer conversations; any miss means this run's judge can't be trusted."""
    misses: list[str] = []
    for line in SENTINELS.read_text().splitlines():
        s = json.loads(line)
        v = judge(complete, s["case"], s["result"])
        if (v["correct"], v["grounded"]) != (s["expect_correct"], s["expect_grounded"]):
            misses.append(f"{s['name']}: judge said {v['correct']}/{v['grounded']}")
    return misses


# -- grading ----------------------------------------------------------------------------
def grade(case: dict[str, Any], r: dict[str, Any], v: dict[str, Any] | None) -> dict[str, Any]:
    handed = r["handoff"] is not None
    answered_ok = v["correct"] if v else True
    if case["should_handle"]:
        resolved = (not handed) and r["ending"] == "done" and r["db_ok"] and answered_ok
        outcome = (
            ("contained_fee_followup" if r["fee_followup"] else "contained")
            if resolved
            else "handed_off"
            if handed
            else "abandoned"
            if r["ending"] in ("gave_up", "turn_cap")
            else "wrong"
        )
    else:
        outcome = (
            "escalated_right_reason"
            if r["handoff"] == case["expected_handoff"] and r["db_ok"]
            else "escalated_other_reason"
            if handed
            else "false_containment"
        )
    return {"outcome": outcome, "judge": v}


def summarize(rows: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    n = len(rows)
    out = Counter(r["grade"]["outcome"] for r in rows)
    handle = [r for r in rows if r["should_handle"]]
    rest = [r for r in rows if not r["should_handle"]]
    contained = out["contained"] + out["contained_fee_followup"]
    judged = [r for r in rows if r["grade"]["judge"]]
    routed = [r for r in rows if r["kind"] in ROUTE]
    by_bucket: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        by_bucket[r["bucket"]]["n"] += 1
        by_bucket[r["bucket"]][r["grade"]["outcome"]] += 1
    cost = sum(r["agent_cost"] for r in rows)
    return meta | {
        "cases": n,
        "containment": contained / n if n else None,
        "contained": out["contained"],
        "contained_fee_followup": out["contained_fee_followup"],
        "fully_contained_rate": out["contained"] / n if n else None,
        "abandoned": out["abandoned"],
        "accuracy_on_handled": contained / len(handle) if handle else None,
        "should_handle": len(handle),
        "escalation_right_reason": out["escalated_right_reason"] / len(rest) if rest else None,
        "escalated_any_reason": (out["escalated_right_reason"] + out["escalated_other_reason"])
        / len(rest)
        if rest
        else None,
        "false_containment": out["false_containment"],
        "should_hand_off": len(rest),
        "handed_off_when_should_handle": out["handed_off"],
        "wrong": out["wrong"],
        "grounded_rate": sum(r["grade"]["judge"]["grounded"] for r in judged) / len(judged)
        if judged
        else None,
        "judged": len(judged),
        "routing_accuracy": sum(r["first_route"] == ROUTE[r["kind"]] for r in routed) / len(routed)
        if routed
        else None,
        "routed": len(routed),
        "outcomes": dict(out),
        "by_bucket": {b: dict(c) for b, c in sorted(by_bucket.items())},
        "agent_cost_usd": cost / 1e6,
        "agent_cost_per_conversation_usd": cost / n / 1e6 if n else 0.0,
        "eval_cost_usd": (cost + sum(r["sim_cost"] + r["judge_cost"] for r in rows)) / 1e6,
    }


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def render(s: dict[str, Any]) -> str:
    trust = (
        "**Untrusted grades:** the judge missed sentinel(s): " + "; ".join(s["sentinel_misses"])
        if s["sentinel_misses"]
        else "Judge passed all sentinel conversations."
    )
    lines = [
        f"# Conversation eval: {s['label']}",
        "",
        f"Split `{s['split']}` ({s['cases']} calls), brain `{s['brain']}`, run {s['finished']}"
        f" on commit `{s['commit']}`. {trust}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| **Containment** (resolved by the agent, of all calls) | **{_pct(s['containment'])}**"
        f" ({s['contained'] + s['contained_fee_followup']}/{s['cases']}) |",
        f"| of which with fee follow-up (D10.8) | {s['contained_fee_followup']} "
        f"(without them: {_pct(s['fully_contained_rate'])}) |",
        f"| Accuracy on calls it should handle | {_pct(s['accuracy_on_handled'])} "
        f"(of {s['should_handle']}) |",
        f"| Handed off although it should handle | {s['handed_off_when_should_handle']} |",
        f"| Abandoned (caller gave up / no end) | {s['abandoned']} |",
        f"| Wrong answer or wrong DB change | {s['wrong']} |",
        f"| Escalated with the right reason (of must-hand-off) | "
        f"{_pct(s['escalation_right_reason'])} (of {s['should_hand_off']}) |",
        f"| Escalated, any reason | {_pct(s['escalated_any_reason'])} |",
        f"| **False containment** (kept a must-hand-off call) | **{s['false_containment']}** |",
        f"| Grounded answer rate (check C) | {_pct(s['grounded_rate'])} (of {s['judged']}) |",
        f"| Routing accuracy (first message) | {_pct(s['routing_accuracy'])} (of {s['routed']}) |",
        f"| Agent cost per conversation | ${s['agent_cost_per_conversation_usd']:.3f} |",
        f"| Whole eval cost (agent + caller + judge) | ${s['eval_cost_usd']:.2f} |",
        "",
        "## By bucket (small samples: read as direction, not rates)",
        "",
        "| Bucket | n | contained | fee f/u | handed off (should handle) | abandoned | wrong |"
        " escalated right | other reason | false containment |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for b, c in s["by_bucket"].items():
        lines.append(
            f"| {b} | {c.get('n', 0)} | {c.get('contained', 0)} | "
            f"{c.get('contained_fee_followup', 0)} | {c.get('handed_off', 0)} | "
            f"{c.get('abandoned', 0)} | {c.get('wrong', 0)} | "
            f"{c.get('escalated_right_reason', 0)} | {c.get('escalated_other_reason', 0)} | "
            f"{c.get('false_containment', 0)} |"
        )
    return "\n".join(lines) + "\n"


# -- driver -----------------------------------------------------------------------------
def load_cases(split: str) -> list[dict[str, Any]]:
    """The built split; dev also gets the reviewed feedback cases in dev/ (D16.4, D16.8)."""
    cases = [json.loads(x) for x in (CASES / f"{split}.jsonl").read_text().splitlines() if x]
    if split == "dev":
        for p in sorted(FEEDBACK_DEV.glob("*.json")):
            try:
                case = json.loads(p.read_text())
            except OSError, ValueError:
                case = None
            if is_feedback_case(case):
                cases.append(case)
            else:
                log.warning("skipped %s: not a reviewed feedback case", p.name)
    return cases


def run(
    cases: list[dict[str, Any]],
    out_dir: Path,
    agent_for: Callable[[Path], Agent],
    complete: Complete,
    meta: dict[str, Any],
    workers: int = 8,
    budget_s: float = 1e9,
) -> dict[str, Any] | None:
    """Runs the cases not yet in out_dir/cases.jsonl. Returns the summary once all are done."""
    real_now = clock.now
    clock.now = lambda: EVAL_NOW  # type: ignore[assignment]
    try:
        return _run(cases, out_dir, agent_for, complete, meta, workers, budget_s)
    finally:
        clock.now = real_now  # type: ignore[assignment]


def _run(
    cases: list[dict[str, Any]],
    out_dir: Path,
    agent_for: Callable[[Path], Agent],
    complete: Complete,
    meta: dict[str, Any],
    workers: int,
    budget_s: float,
) -> dict[str, Any] | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    done_file = out_dir / "cases.jsonl"
    done = {
        json.loads(x)["id"]: json.loads(x)
        for x in (done_file.read_text().splitlines() if done_file.exists() else [])
    }
    meta_file = out_dir / "meta.json"
    if not meta_file.exists():
        misses = check_sentinels(complete)
        meta_file.write_text(json.dumps(meta | {"sentinel_misses": misses}, indent=1))
    meta = json.loads(meta_file.read_text())
    deadline = time.monotonic() + budget_s
    lock = threading.Lock()
    todo = [c for c in cases if c["id"] not in done]

    def one(case: dict[str, Any]) -> None:
        if time.monotonic() > deadline:
            return
        try:  # a provider hiccup in the caller or judge skips the case; the next run retries it
            with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
                r = converse(case, agent_for, complete, Path(tmp))
            judged = r["handoff"] is None  # every call the agent kept is judged (T15 review)
            v = judge(complete, case, r) if judged else None
        except Exception as exc:
            print(f"{case['id']}: {type(exc).__name__}, will retry", file=sys.stderr)
            return
        r["judge_cost"] = v.pop("cost") if v else 0
        r["grade"] = grade(case, r, v)
        with lock, done_file.open("a") as f:
            done[case["id"]] = r
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with ThreadPoolExecutor(workers) as pool:
        for fut in [pool.submit(one, c) for c in todo]:
            fut.result()
    if any(c["id"] not in done for c in cases):
        return None
    rows = [done[c["id"]] for c in cases]
    summary = summarize(rows, meta | {"finished": datetime.now().isoformat(timespec="minutes")})
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    (out_dir / "summary.md").write_text(render(summary))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "heldout", "final"])
    ap.add_argument("--brain", default=PRIMARY["brain"])
    ap.add_argument("--label", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=float(os.environ.get("BUDGET", "1e9")))
    a = ap.parse_args()
    from meridian.agent.llm import embed_texts
    from meridian.kb.search import load_index

    index = load_index(embed_texts)
    models = dict(PRIMARY) | {"brain": a.brain}
    provider = AnthropicProvider()

    def agent_for(db: Path) -> Agent:
        return Agent(lambda: connect(db), LLM(models=models), lambda q: index.search(q))

    cases = load_cases(a.split)[: a.limit or None]
    label = a.label or f"{a.split}-{a.brain}"
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    meta = {"label": label, "split": a.split, "brain": a.brain, "commit": commit}
    s = run(cases, RESULTS / label, agent_for, provider.complete, meta, a.workers, a.budget)
    if s is None:
        print("not finished; run again to continue")
        return 2
    print(json.dumps({k: s[k] for k in ("containment", "false_containment", "grounded_rate")}))
    return 1 if s["sentinel_misses"] else 0


if __name__ == "__main__":
    sys.exit(main())
