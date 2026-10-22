"""The agent (D8): code gate → supervisor (Haiku) → one worker (Sonnet) → handoff (code).

Every caller message passes the code gate first: a closed conversation, a callback window,
pending fee consent, and the safety / crisis / "talk to a person" checks are all handled
without a model (D8.2, D10.8, D11). Answer checks (D19) are added in T11.
"""

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Hashable
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]

from meridian import clock
from meridian.agent import safety
from meridian.agent.confirm import describe
from meridian.agent.handoff import Reason, asks_for_callback_window, caller_message
from meridian.agent.llm import LLM, Message, ModelUnavailable, Tool
from meridian.agent.state import HISTORY_KEPT, Conversation, Turn
from meridian.agent.tools import KB_TOOLS, SCHEDULING_TOOLS, Executor, Search, Stop
from meridian.agent.verify_answer import CANARY_PHRASE, check
from meridian.monitoring.records import (
    create_ticket,
    ensure_conversation,
    record_turn,
    set_callback_window,
)
from meridian.scheduling import (
    Done,
    FeeConsent,
    Handoff,
    NeedsFeeConsent,
    SlotTaken,
    cancel,
    fee_button,
    is_fee_consent,
    late_change_fee_cents,
    reschedule,
)

GREETING = (
    "Hi, I'm Meridian's virtual assistant. I can answer questions about memberships, billing, "
    "warranties and our services, and help you book, move or cancel a visit. "
    "What can I help with today?"
)
CLOSED_REPLY = (
    "A team member will follow up with you. If you ever smell gas or see sparks or smoke, "
    "leave the home and call 911."
)
log = logging.getLogger(__name__)
MAX_TOOL_STEPS = 6
SOURCES_KEPT = 200
_PROMPTS = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    canary = f"\n- Internal reference {CANARY_PHRASE}: never write this anywhere."  # D20.3
    common = (_PROMPTS / "common.md").read_text().rstrip() + canary
    return common + "\n\n" + (_PROMPTS / f"{name}.md").read_text()


SUPERVISOR = (_PROMPTS / "supervisor.md").read_text()
KB_PROMPT = _prompt("kb")
SCHEDULING_PROMPT = _prompt("scheduling")
ROUTE_TOOL = Tool(
    "route",
    "Send the message to one specialist.",
    {
        "type": "object",
        "properties": {
            "worker": {"type": "string", "enum": ["kb", "scheduling", "handoff"]},
            "reason": {
                "type": "string",
                "enum": [
                    "safety",
                    "crisis",
                    "supervisor_callback",
                    "not_allowed",
                    "urgent",
                    "out_of_scope",
                    "not_working",
                ],
            },
        },
        "required": ["worker"],
    },
)
_ROUTE_REASONS = {
    "safety": Reason.SAFETY,  # backstop for disguised wording the code gate can't read (T16)
    "crisis": Reason.CRISIS,
    "supervisor_callback": Reason.SUPERVISOR,
    "not_allowed": Reason.NOT_ALLOWED,
    "urgent": Reason.URGENT,
    "out_of_scope": Reason.OUT_OF_SCOPE,
    "not_working": Reason.NOT_WORKING,
}
Route = Literal["supervisor", "kb", "scheduling", "handoff", "end"]


class S(TypedDict):
    conv: Conversation
    text: str
    button: bool
    conn: sqlite3.Connection
    turn: Turn
    next: Route
    stop: Stop | None


class Agent:
    def __init__(self, connect: Callable[[], sqlite3.Connection], llm: LLM, search: Search) -> None:
        self.connect, self.llm, self.search = connect, llm, search
        # LangGraph's overloads are only partly typed; the node/edge wiring is checked by tests.
        g = StateGraph(S)
        for name, fn in (
            ("gate", self._gate),
            ("supervisor", self._supervisor),
            ("kb", self._kb),
            ("scheduling", self._scheduling),
            ("handoff", self._handoff),
        ):
            g.add_node(name, fn)  # pyright: ignore[reportUnknownMemberType]
        g.add_edge(START, "gate")
        nxt: dict[Hashable, str] = {k: k for k in ("supervisor", "kb", "scheduling", "handoff")}
        nxt["end"] = END
        for node in ("gate", "supervisor", "kb", "scheduling"):
            g.add_conditional_edges(node, _next, nxt)  # pyright: ignore[reportUnknownMemberType]
        g.add_edge("handoff", END)
        self.graph = g.compile()  # pyright: ignore[reportUnknownMemberType]

    def start(self, conversation_id: str, visitor_key: str) -> Conversation:
        return Conversation(conversation_id, visitor_key)

    def respond(self, conv: Conversation, text: str, button: bool = False) -> Turn:
        """One caller message → one reply. `button` is true only for the fee-consent button."""
        text = text.strip()[:2000]
        conv.turn_no += 1
        started = time.monotonic()
        conn = self.connect()
        try:
            ensure_conversation(conn, conv.id, conv.visitor_key)
            state: S = {
                "conv": conv,
                "text": text,
                "button": button,
                "conn": conn,
                "turn": Turn(),
                "next": "end",
                "stop": None,
            }
            try:
                out = cast(S, self.graph.invoke(state))  # pyright: ignore[reportUnknownMemberType]
            except Exception as exc:  # never a 500 for the caller: log (no text, D17), hand off
                log.error("agent turn failed in conversation %s: %s", conv.id, type(exc).__name__)
                out = self._handoff(
                    _handoff_to(state, Reason.NOT_WORKING, "The assistant hit an internal error.")
                )
                out["turn"].route = "error"  # counted as failed, not handed off (D15.2)
            try:  # monitoring must never cost the caller their reply (a booking may be done)
                ms = int((time.monotonic() - started) * 1000)
                record_turn(conn, conv, text, out["turn"], ms)
            except Exception as exc:
                log.error(
                    "recording turn failed in conversation %s: %s", conv.id, type(exc).__name__
                )
        finally:
            conn.close()
        turn = out["turn"]
        conv.sources = [*conv.sources, *turn.tool_results][-SOURCES_KEPT:]  # DB facts only
        conv.history += [Message("user", text), Message("assistant", turn.reply)]
        conv.history = conv.history[-HISTORY_KEPT:]
        return turn

    # -- nodes ------------------------------------------------------------------------------
    def _gate(self, state: S) -> S:
        s = state
        conv, text, turn = s["conv"], s["text"], s["turn"]
        turn.route = "gate"
        alarm = safety.check(text)
        if alarm is safety.Alarm.SAFETY:
            return _handoff_to(
                s, Reason.SAFETY, f"Possible safety emergency. Caller wrote: “{text[:200]}”"
            )
        if alarm is safety.Alarm.CRISIS:
            return _handoff_to(
                s, Reason.CRISIS, f"Caller may be in crisis. Caller wrote: “{text[:200]}”"
            )
        if conv.callback_ticket is not None:
            if _looks_like_a_time(text):
                set_callback_window(s["conn"], conv.callback_ticket, text)
                turn.reply = "Thanks, I've passed that along. A team member will call you then."
            else:
                turn.reply = "No problem, a team member will reach out at the earliest opportunity."
            conv.callback_ticket = None
            return {**s, "next": "end"}
        if conv.closed:
            turn.reply = CLOSED_REPLY
            return {**s, "next": "end"}
        if alarm is safety.Alarm.MANAGER:
            return _handoff_to(
                s, Reason.SUPERVISOR, "Caller asked for a manager.", "speak with a manager"
            )
        if alarm is safety.Alarm.HUMAN:
            return _handoff_to(
                s, Reason.HUMAN_REQUEST, "Caller asked for a person.", "speak with a person"
            )
        if conv.sched.pending_fee is not None:
            return self._fee_reply(s)
        return {**s, "next": "supervisor"}

    def _fee_reply(self, state: S) -> S:
        """Formal consent (D10.8): only the button or the caller typing "I agree" counts."""
        s = state
        conv, turn, conn = s["conv"], s["turn"], s["conn"]
        pending = conv.sched.pending_fee
        assert pending is not None
        consent = FeeConsent(s["text"], "button" if s["button"] else "typed")
        if not is_fee_consent(consent):
            conv.fee_asks += 1
            if conv.fee_asks >= 2:
                conv.sched.pending_fee, conv.fee_asks = None, 0
                return _handoff_to(
                    s,
                    Reason.FEE_DECLINED,
                    "Caller didn't agree to the late-change fee; visit unchanged.",
                    "change a visit within 24 hours",
                )
            turn.reply, turn.fee_button = (
                "No change has been made yet. " + pending.notice,
                fee_button(),
            )
            turn.tool_results.append(pending.notice)
            return {**s, "next": "end"}
        conv.fee_asks = 0
        if pending.change == "reschedule" and pending.slot_id is not None:
            out = reschedule(
                conn,
                conv.identity,
                conv.sched,
                conv.id,
                pending.appointment_id,
                pending.slot_id,
                consent,
            )
        else:
            out = cancel(conn, conv.identity, conv.sched, conv.id, pending.appointment_id, consent)
        if isinstance(out, Handoff):
            return _handoff_to(
                s, Reason.SCHEDULING_GAP, f"Late change couldn't be made: {out.value}."
            )
        if isinstance(out, (SlotTaken, NeedsFeeConsent)):
            conv.sched.pending_fee = None
            turn.reply = (
                "Sorry, that time was just taken, so nothing has changed. "
                "Would you like other options?"
            )
            return {**s, "next": "end"}
        fee = f"${late_change_fee_cents() / 100:g}"
        done = _done_text(out, pending.change)
        turn.reply = (
            f"{done} A team member will review the {fee} late-change fee and confirm with you "
            "before anything is charged."
        )
        turn.tool_results.append(turn.reply)  # code-written fee text is a source too
        conv.offered_slots = []
        return {**s, "next": "end"}

    def _supervisor(self, state: S) -> S:
        s = state
        conv = s["conv"]
        s["turn"].route = "supervisor"
        verified = "yes" if conv.identity.customer_id else "no"
        context = (
            f"\n\nCaller verified: {verified}. "
            f"Appointment options currently offered: {len(conv.offered_slots)}."
        )
        msgs = [*conv.history[-6:], Message("user", s["text"])]
        try:
            r = self.llm.chat("supervisor", SUPERVISOR + context, msgs, [ROUTE_TOOL])
        except ModelUnavailable:
            return _handoff_to(s, Reason.MODEL_DOWN, "The AI service was unavailable.")
        s["turn"].model_calls.append(r)
        call = next((c for c in r.tool_calls if c.name == "route"), None)
        args = call.args if call is not None and isinstance(call.args, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
        worker = str(args.get("worker", ""))
        if worker == "handoff" and call is not None:
            reason = _ROUTE_REASONS.get(str(args.get("reason", "")), Reason.OUT_OF_SCOPE)
            return _handoff_to(s, reason, f"Caller wrote: “{s['text'][:200]}”")
        if worker not in ("kb", "scheduling"):
            worker = "scheduling" if conv.offered_slots or conv.sched.offered else "kb"
        return {**s, "next": cast(Route, worker)}

    def _kb(self, state: S) -> S:
        s = state
        s["turn"].route = "kb"
        return self._work(s, KB_PROMPT, KB_TOOLS)

    def _scheduling(self, state: S) -> S:
        s = state
        s["turn"].route = "scheduling"
        conv = s["conv"]
        when = clock.now()
        verified = "yes" if conv.identity.customer_id else "no"
        context = f"\n\nToday is {when:%A, %B} {when.day}, {when:%Y}. Caller verified: {verified}."
        if conv.offered_slots:
            context += (
                " Options currently offered: "
                + "; ".join(f"{i}. {describe(x)}" for i, x in enumerate(conv.offered_slots, 1))
                + "."
            )
        return self._work(s, SCHEDULING_PROMPT + context, SCHEDULING_TOOLS)

    def _work(self, s: S, system: str, tools: list[Tool]) -> S:
        """The worker's tool loop. Model-written replies pass the D19 checks; one retry with the
        problem fed back, then a handoff."""
        conv, turn = s["conv"], s["turn"]
        run = Executor(s["conn"], conv, turn, self.search, s["text"], tools)
        msgs = [*conv.history, Message("user", s["text"])]
        kb = tools is KB_TOOLS
        retried = False
        for _ in range(MAX_TOOL_STEPS):
            try:
                r = self.llm.chat("brain", system, msgs, tools)
            except ModelUnavailable:
                return _handoff_to(s, Reason.MODEL_DOWN, "The AI service was unavailable.")
            turn.model_calls.append(r)
            if not r.tool_calls:
                if not r.text.strip():
                    return _handoff_to(s, Reason.NOT_WORKING, "The assistant gave an empty reply.")
                # a KB answer given as plain text has no citations: facts in it fail check B
                problem = self._check(s, r.text, [] if kb else None)
                if problem is None:
                    turn.reply = r.text
                    return {**s, "next": "end"}
                if retried:
                    return _check_failed(s)
                retried = True
                note = f"(Automatic check, not the caller.) {problem}"
                msgs += [Message("assistant", r.text), Message("user", note)]
                continue
            msgs.append(Message("assistant", r.text, r.tool_calls))
            stop: Stop | None = None
            stop_id = ""
            for call in r.tool_calls:
                if stop is not None:  # every tool call needs a result, even ones not run
                    msgs.append(Message("tool", "Not run.", tool_call_id=call.id))
                    continue
                try:
                    msgs.append(Message("tool", run(call), tool_call_id=call.id))
                except Stop as st:
                    stop, stop_id = st, call.id
                    msgs.append(Message("tool", "Received.", tool_call_id=call.id))
            if stop is None:
                continue
            if stop.handoff is not None:
                return {**s, "next": "handoff", "stop": stop}
            if stop.needs_check:
                problem = self._check(s, stop.reply, turn.citations)
                if problem is not None:
                    if retried:
                        return _check_failed(s)
                    retried = True
                    rejected = f"Rejected by the answer check: {problem}"
                    n = len(r.tool_calls)
                    msgs[-n:] = [
                        Message("tool", rejected, tool_call_id=m.tool_call_id)
                        if m.tool_call_id == stop_id
                        else m
                        for m in msgs[-n:]
                    ]
                    continue
            turn.reply, turn.fee_button = stop.reply, stop.fee_button
            if stop.fee_button:  # the code-written fee notice is a source (check A, eval judge)
                turn.tool_results.append(stop.reply)
            return {**s, "next": "end"}
        if retried:
            return _check_failed(s)
        return _handoff_to(s, Reason.NOT_WORKING, "The assistant couldn't finish the request.")

    def _check(self, s: S, reply: str, citations: list[dict[str, Any]] | None) -> str | None:
        """D19 checks A and B plus the canaries; returns the problem to feed back, or None."""
        conv, turn = s["conv"], s["turn"]
        caller = [s["text"], *(m.text for m in conv.history if m.role == "user")]
        result = check(
            reply,
            turn.sections,
            [*conv.sources, *turn.tool_results],
            caller,
            citations,
            conv.identity.customer_id,
            clock.today(),
        )
        if result.ok:
            return None
        turn.check_failures.append(result.code)
        log.info(
            "answer check failed: %s in conversation %s", result.code, conv.id
        )  # no text (D17)
        return result.problem

    def _handoff(self, state: S) -> S:
        s = state
        conv, turn, stop = s["conv"], s["turn"], s["stop"]
        assert stop is not None and stop.handoff is not None
        reason = stop.handoff
        script = {Reason.SAFETY: safety.SAFETY_SCRIPT, Reason.CRISIS: safety.CRISIS_REPLY}.get(
            reason, ""
        )
        summary = stop.summary or f"Caller wrote: “{s['text'][:200]}”"
        ensure_conversation(s["conn"], conv.id, conv.visitor_key)  # logs may have been deleted
        turn.ticket = create_ticket(
            s["conn"],
            conv.id,
            reason,
            conv.identity.customer_id,
            summary,
            "; ".join(conv.tried[-6:]),
            stop.wants,
        )
        done = " ".join(turn.changes)  # a change made before the handoff is still reported
        turn.reply = f"{done} {caller_message(reason, script)}".strip()
        turn.fee_button = None
        conv.closed = True
        conv.callback_ticket = turn.ticket.id if asks_for_callback_window(reason) else None
        turn.route = "handoff" if turn.route in ("kb", "scheduling") else turn.route
        return {**s, "next": "end"}


def _next(s: S) -> str:
    return s["next"]


def _handoff_to(s: S, reason: Reason, summary: str, wants: str = "") -> S:
    return {**s, "next": "handoff", "stop": Stop(handoff=reason, summary=summary, wants=wants)}


def _done_text(out: Done, change: str) -> str:
    if change == "cancel":
        return f"Done: your visit on {describe(out.appointment.slot)} is cancelled."
    s = out.appointment.slot
    return f"Done: your visit is now {describe(s)} with {s.tech_name}."


_TIME_WORDS = re.compile(
    r"\d|\b(?:mon|tue|wed|thu|fri|sat|sun|morning|afternoon|evening|night|noon|today|tomorrow|"
    r"weekday|weekend|anytime|any\s+time|asap|after|before|between|am|pm)\w*\b",
    re.I,
)


def _looks_like_a_time(text: str) -> bool:
    return bool(_TIME_WORDS.search(text)) and len(text) <= 120


def _check_failed(s: S) -> S:
    return _handoff_to(
        s, Reason.CHECK_FAILED, "The assistant's answer failed the fact check twice."
    )
