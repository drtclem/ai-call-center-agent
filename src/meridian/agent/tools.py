"""Tools the workers can ask for, and the code that runs them (D8.4–5, D20.1).

Every argument from the model is validated here; customer identity always comes from the
session (rule 6); booking needs the caller's own words to agree (D10.6, `confirm.py`); fee
consent never comes from a tool (D10.8, handled in the graph's gate).
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from meridian import limits
from meridian.agent import confirm
from meridian.agent.handoff import Reason
from meridian.agent.llm import Tool, ToolCall
from meridian.agent.state import Conversation, Turn
from meridian.identity import NO_MATCH, NotVerified, Outcome, verify
from meridian.kb.search import Hit
from meridian.scheduling import (
    EVENING,
    SERVICE_TYPES,
    Done,
    Handoff,
    NeedsFeeConsent,
    NotFound,
    NotOffered,
    Slot,
    SlotStateMismatch,
    SlotTaken,
    book,
    cancel,
    fee_button,
    find_slots,
    get_appointments,
    get_invoices,
    get_membership,
    reschedule,
)

Search = Callable[[str], list[Hit]]
WINDOWS = ("08:00-12:00", "12:00-16:00", "13:00-17:00", EVENING)
TROUBLESHOOTING_DOC = "faq/troubleshooting_before_you_call.md"
SERVICE_NAMES = {
    "hvac_repair": "repair visit",
    "hvac_tune_up": "tune-up",
    "plumbing": "plumbing visit",
    "estimate": "replacement estimate",
}
_HANDOFF_REASONS = {
    "supervisor_callback": Reason.SUPERVISOR,
    "not_allowed": Reason.NOT_ALLOWED,
    "no_supporting_source": Reason.NO_SOURCE,
    "out_of_scope": Reason.OUT_OF_SCOPE,
    "urgent": Reason.URGENT,
    "not_working": Reason.NOT_WORKING,
    "cannot_be_booked_here": Reason.SCHEDULING_GAP,
}


@dataclass
class Stop(Exception):
    """Ends the worker's turn: a code-written reply, a handoff, or the model's final answer."""

    reply: str = ""
    handoff: Reason | None = None
    summary: str = ""
    wants: str = ""
    fee_button: str | None = None
    needs_check: bool = False  # a model-written answer: run the D19 checks before sending


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_STR = {"type": "string"}
HANDOFF_TOOL = Tool(
    "request_handoff",
    "Pass the conversation to a person when you can't or shouldn't finish it.",
    _schema(
        {
            "reason": {"type": "string", "enum": sorted(_HANDOFF_REASONS)},
            "summary": {"type": "string", "description": "2-3 sentences for the staff member"},
            "wants": {"type": "string", "description": "what the caller wants, in a few words"},
        },
        ["reason", "summary", "wants"],
    ),
)
KB_TOOLS = [
    Tool(
        "search_kb",
        "Search Meridian's current customer documents.",
        _schema({"query": _STR}, ["query"]),
    ),
    Tool(
        "answer",
        "Give the final answer, citing each fact.",
        _schema(
            {
                "text": _STR,
                "citations": {
                    "type": "array",
                    "items": _schema({"section_id": _STR, "quote": _STR}, ["section_id", "quote"]),
                },
            },
            ["text", "citations"],
        ),
    ),
    HANDOFF_TOOL,
]
SCHEDULING_TOOLS = [
    KB_TOOLS[0],  # search_kb: side questions mid-booking (fees, warranty) without a handoff
    Tool(
        "verify_identity",
        "Verify the caller: full name plus one of account number, phone, or service address.",
        _schema({"name": _STR, "account_number": _STR, "phone": _STR, "address": _STR}, ["name"]),
    ),
    Tool(
        "find_slots",
        "Find bookable appointment options for the verified caller.",
        _schema(
            {
                "service_type": {"type": "string", "enum": list(SERVICE_TYPES)},
                "date": {"type": "string", "description": "preferred date, YYYY-MM-DD"},
                "window": {"type": "string", "enum": list(WINDOWS)},
                "earlier": {
                    "type": "boolean",
                    "description": "the caller asked for anything earlier",
                },
            },
            ["service_type"],
        ),
    ),
    Tool(
        "book_slot",
        "Book an offered option the caller clearly agreed to.",
        _schema({"option": {"type": "integer"}, "issue": _STR}, ["option", "issue"]),
    ),
    Tool("get_appointments", "The verified caller's upcoming visits.", _schema({}, [])),
    Tool(
        "reschedule_visit",
        "Move a visit to an offered option the caller clearly agreed to.",
        _schema(
            {"appointment_id": {"type": "integer"}, "option": {"type": "integer"}},
            ["appointment_id", "option"],
        ),
    ),
    Tool(
        "cancel_visit",
        "Cancel one of the caller's visits.",
        _schema({"appointment_id": {"type": "integer"}}, ["appointment_id"]),
    ),
    Tool("get_invoices", "The verified caller's invoices (read-only).", _schema({}, [])),
    Tool(
        "get_membership", "The verified caller's membership tier and renewal date.", _schema({}, [])
    ),
    Tool(
        "quick_checks",
        "Quick checks a caller can try before a visit.",
        _schema({"problem": _STR}, ["problem"]),
    ),
    HANDOFF_TOOL,
]


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _slot_line(n: int, s: Slot) -> str:
    return f"{n}. {confirm.describe(s)} — {SERVICE_NAMES[s.service_type]} with {s.tech_name}"


def _asked_line(on: date | None, window: str, out: list[Slot]) -> str:
    """Whether the exact day (and window) the caller asked for is open, said by code, so the
    reply never has to guess it (live probe: "no Saturday openings" when there was one)."""
    if on is None:
        return ""
    asked = f"{on:%a %b} {on.day}" + (f", {confirm.window_text(window)}" if window else "")
    hit = any(s.date == on and (not window or s.window == window) for s in out)
    return f"Asked for {asked}: " + ("open, below.\n" if hit else "nothing open then.\n")


def _visit(s: Slot) -> str:
    return f"{confirm.describe(s)}, {SERVICE_NAMES[s.service_type]}, technician {s.tech_name}"


WRITE_TOOLS = frozenset({"book_slot", "reschedule_visit", "cancel_visit"})
MAX_TOOL_CALLS = 12  # per turn, across all model rounds (cost guard)


class Executor:
    """Runs one worker's tool calls for one turn: only that worker's tools, at most one change."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        conv: Conversation,
        turn: Turn,
        search: Search,
        user_text: str,
        allowed: list[Tool],
    ) -> None:
        self.conn, self.conv, self.turn, self.search, self.text = (
            conn,
            conv,
            turn,
            search,
            user_text,
        )
        self.allowed = {t.name for t in allowed}

    def __call__(self, call: ToolCall) -> str:
        self.turn.tool_calls.append(call.name)
        if call.name not in self.allowed:
            return "Unknown tool."
        if len(self.turn.tool_calls) > MAX_TOOL_CALLS:
            raise Stop(handoff=Reason.NOT_WORKING, summary="Too many steps in one turn.")
        if call.name in WRITE_TOOLS and self.turn.changes:
            return "Not run: only one change per message. Tell the caller what was done first."
        if not isinstance(call.args, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            return "That request wasn't valid."
        fn = getattr(self, f"t_{call.name}")
        try:
            out = str(fn(**call.args))
        except Stop:
            raise
        except NotVerified:
            return (
                "Not verified yet: ask for their full name plus account number, phone "
                "or service address."
            )
        except TypeError, ValueError, KeyError, NotOffered, NotFound:
            return "That request wasn't valid. Check the options or visit ids and try again."
        except SlotStateMismatch:
            raise Stop(
                handoff=Reason.SCHEDULING_GAP, summary="A visit's slot record looked inconsistent."
            ) from None
        self.turn.tool_results.append(out)
        if call.name in WRITE_TOOLS and out.startswith(("Booked:", "Moved:", "Cancelled:")):
            self.turn.changes.append(out)
        return out

    # -- shared --------------------------------------------------------------------------
    def t_request_handoff(self, reason: str, summary: str = "", wants: str = "") -> str:
        raise Stop(
            handoff=_HANDOFF_REASONS.get(reason, Reason.OUT_OF_SCOPE), summary=summary, wants=wants
        )

    def _search(self, query: str, doc: str | None = None) -> str:
        hits = [h for h in self.search(str(query)[:300]) if doc is None or h.chunk.doc_path == doc]
        self.conv.tried.append(f"Searched: {str(query)[:80]}")
        for h in hits:
            self.turn.sections[h.chunk.id] = h.chunk.text
        return (
            "\n\n".join(f"[{h.chunk.id}]\n{h.chunk.text}" for h in hits) or "No matching sections."
        )

    # -- KB worker ------------------------------------------------------------------------
    def t_search_kb(self, query: str) -> str:
        return self._search(query)

    def t_answer(self, text: str, citations: list[dict[str, Any]] | None = None) -> str:
        self.turn.citations = [c for c in (citations or []) if isinstance(c, dict)]  # pyright: ignore[reportUnnecessaryIsInstance]
        raise Stop(reply=str(text), needs_check=True)

    # -- scheduling worker ------------------------------------------------------------------
    def _account(self) -> None:
        if self.conv.identity.handed_off:
            raise Stop(handoff=Reason.IDENTITY_FAILED, summary="Identity could not be verified.")
        self.conv.identity.require_customer()

    def t_verify_identity(
        self, name: str, account_number: str = "", phone: str = "", address: str = ""
    ) -> str:
        if self.conv.identity.customer_id is None and limits.identity_blocked(
            self.conn, self.conv.visitor_key
        ):
            limits.count_hit(self.conn, self.conv.visitor_key, "identity_cap")
            raise Stop(
                handoff=Reason.IDENTITY_FAILED, summary="Too many failed identity checks today."
            )
        before = self.conv.identity.failures
        r = verify(
            self.conn,
            self.conv.identity,
            str(name),
            account_number or None,
            phone or None,
            address or None,
        )
        if self.conv.identity.failures > before:  # T13: failures count per IP across chats
            limits.record(self.conn, self.conv.visitor_key, "identity_fail")
        if r.outcome is Outcome.VERIFIED:
            return "Verified."
        if r.outcome is Outcome.HANDOFF:
            raise Stop(
                handoff=Reason.IDENTITY_FAILED,
                summary="Identity check failed or was ambiguous.",
                wants="account help",
            )
        if r.outcome is Outcome.ASK_FACTOR:
            return (
                "Ask for one of: account number, phone number on the account, or service address."
            )
        return f'Not verified. Say exactly: "{NO_MATCH}"'

    def t_find_slots(
        self, service_type: str, date: str = "", window: str = "", earlier: bool = False
    ) -> str:
        self._account()
        on = _parse_date(date)
        if window and window not in WINDOWS:
            raise ValueError("window")
        limit: tuple[Any, str] | None = None
        if earlier:
            if self.conv.offered_slots:
                first = min(self.conv.offered_slots, key=lambda s: (s.date, s.window))
                limit = (first.date, first.window)
            else:
                appts = get_appointments(self.conn, self.conv.identity)
                if appts:
                    limit = (appts[0].slot.date, appts[0].slot.window)
        out = find_slots(
            self.conn, self.conv.identity, self.conv.sched, service_type, on, window or None, limit
        )
        if out is Handoff.SAME_DAY:  # D10: no same-day booking, but don't drop the caller
            return (
                "Same-day (or past) dates can't be booked here. Tell the caller, and offer the "
                "soonest days instead (call find_slots without a date). Hand off only if they "
                "need someone today."
            )
        if isinstance(out, Handoff):
            raise Stop(
                handoff=Reason.SCHEDULING_GAP,
                summary=f"Can't book here: {out.value}.",
                wants=f"{SERVICE_NAMES.get(service_type, service_type)}",
            )
        if not out:
            return "No earlier openings. Tell the caller their current option or visit stays as is."
        asked = (
            "" if limit else _asked_line(on, window, out)
        )  # with `earlier`, the day isn't searched
        if [s.slot_id for s in out] == [s.slot_id for s in self.conv.offered_slots]:
            # the same options again: the caller already saw them, so an agreement still counts
            return (
                asked
                + "Same options as already offered:\n"
                + "\n".join(_slot_line(i, s) for i, s in enumerate(out, 1))
            )
        self.conv.offered_slots, self.conv.offered_turn = list(out), self.conv.turn_no
        self.conv.readback = None
        self.conv.tried.append("Offered: " + "; ".join(confirm.describe(s) for s in out))
        return (
            asked
            + "Options to offer, numbered exactly like this:\n"
            + ("\n".join(_slot_line(i, s) for i, s in enumerate(out, 1)))
        )

    def _agreed(self, option: int, verb: str = "book") -> str | None:
        """None if the caller's own words, in reply to options shown in an earlier message, agree
        to this option; otherwise what to ask them (D10.6)."""
        conv, offered = self.conv, self.conv.offered_slots
        if not 1 <= option <= len(offered):
            raise ValueError("option")
        s = offered[option - 1]
        if conv.offered_turn >= conv.turn_no:
            return "Not booked: the caller hasn't seen these options yet. Offer them first."
        readback = conv.readback if conv.readback_turn == conv.turn_no - 1 else None
        if confirm.agreed_option(self.text, offered, readback) == option:
            return None
        if readback == option:
            conv.readback = None
            return (
                "Not booked: the caller still hasn't clearly confirmed. "
                "Ask what they'd like to do instead."
            )
        conv.readback, conv.readback_turn = option, conv.turn_no
        # code sends the question itself, so the caller's next "yes" can only answer this
        raise Stop(reply=f"Just to confirm: {verb} {_visit(s)}?")

    def t_book_slot(self, option: int, issue: str) -> str:
        self._account()
        ask = self._agreed(int(option))
        if ask:
            return ask
        if not limits.bookings_left(self.conn, self.conv.visitor_key):
            limits.count_hit(self.conn, self.conv.visitor_key, "bookings_per_day")
            raise Stop(
                handoff=Reason.NOT_ALLOWED, summary="Daily booking limit for this visitor reached."
            )
        s = self.conv.offered_slots[int(option) - 1]
        out = book(self.conn, self.conv.identity, self.conv.sched, s.slot_id, str(issue))
        if not isinstance(out, SlotTaken):
            limits.record(self.conn, self.conv.visitor_key, "booking")
        self.conv.readback = None
        if isinstance(out, SlotTaken):
            self.conv.offered_slots = [x for x in self.conv.offered_slots if x.slot_id != s.slot_id]
            return "That time was just taken by someone else. Apologize and call find_slots again."
        self.conv.offered_slots = []
        a = out.appointment
        warranty = (
            " Warranty visit: no diagnostic fee if the technician confirms the failure is covered."
            if a.is_warranty_visit
            else ""
        )
        return f"Booked: {_visit(a.slot)}.{warranty}"

    def t_get_appointments(self) -> str:
        self._account()
        appts = get_appointments(self.conn, self.conv.identity)
        lines = [f"visit id {a.appointment_id}: {_visit(a.slot)}" for a in appts]
        return "\n".join(lines) or "No upcoming visits."

    def _fee_or_done(self, out: Done | SlotTaken | NeedsFeeConsent | Handoff, verb: str) -> str:
        if isinstance(out, NeedsFeeConsent):
            raise Stop(reply=out.message, fee_button=fee_button())
        if isinstance(out, Handoff):
            raise Stop(
                handoff=Reason.SCHEDULING_GAP,
                summary=f"Can't change this visit here: {out.value}.",
                wants=f"{verb} a visit",
            )
        if isinstance(out, SlotTaken):
            return "That time was just taken by someone else. Apologize and call find_slots again."
        a = out.appointment
        return f"{'Moved' if verb == 'move' else 'Cancelled'}: {_visit(a.slot)}."

    def t_reschedule_visit(self, appointment_id: int, option: int) -> str:
        self._account()
        ask = self._agreed(int(option), "move your visit to")
        if ask:
            return ask
        s = self.conv.offered_slots[int(option) - 1]
        out = reschedule(
            self.conn,
            self.conv.identity,
            self.conv.sched,
            self.conv.id,
            int(appointment_id),
            s.slot_id,
        )
        if isinstance(out, Done):
            self.conv.offered_slots = []
        return self._fee_or_done(out, "move")

    def t_cancel_visit(self, appointment_id: int) -> str:
        """Two steps: ask the caller to confirm this visit, then cancel on their clear yes."""
        self._account()
        conv, appt = self.conv, int(appointment_id)
        confirmed = (
            conv.pending_cancel == appt
            and conv.pending_cancel_turn == conv.turn_no - 1
            and confirm.classify(self.text) is confirm.Reply.AGREES
        )
        if not confirmed:
            visits = get_appointments(self.conn, conv.identity)
            visit = next((a for a in visits if a.appointment_id == appt), None)
            if visit is None:
                raise NotFound
            conv.pending_cancel, conv.pending_cancel_turn = appt, conv.turn_no
            raise Stop(reply=f"Just to confirm: cancel your {_visit(visit.slot)}?")
        conv.pending_cancel = None
        out = cancel(self.conn, conv.identity, conv.sched, conv.id, appt)
        return self._fee_or_done(out, "cancel")

    def t_get_invoices(self) -> str:
        self._account()
        lines: list[str] = []
        for inv in get_invoices(self.conn, self.conv.identity):
            items = "; ".join(
                f"{i.get('desc', '')} {_money(int(i.get('cents', 0)))}"
                for i in _items(inv.line_items)
            )
            lines.append(
                f"{inv.invoice_id}: issued {inv.issued}, due {inv.due}, "
                f"total {_money(inv.amount_cents)}, balance {_money(inv.balance_cents)}. "
                f"Items: {items}"
            )
        return "\n".join(lines) or "No invoices on file."

    def t_get_membership(self) -> str:
        self._account()
        tier, renewal = get_membership(self.conn, self.conv.identity)
        if tier == "none":
            return "Not a Comfort Club member."
        return f"Comfort Club {'Plus' if tier == 'plus' else 'Standard'}, renews {renewal}."

    def t_quick_checks(self, problem: str) -> str:
        return self._search(problem, TROUBLESHOOTING_DOC)


def _parse_date(s: str) -> date | None:
    return date.fromisoformat(s) if s else None


def _items(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []  # pyright: ignore[reportUnknownVariableType]
