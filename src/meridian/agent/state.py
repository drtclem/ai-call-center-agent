"""Per-conversation state shared by every node of the graph (D8.6)."""

from dataclasses import dataclass, field
from typing import Any

from meridian.agent.handoff import Ticket
from meridian.agent.llm import Message, Reply
from meridian.identity import IdentitySession
from meridian.scheduling import SchedulingState, Slot

HISTORY_KEPT = 24  # caller/assistant messages sent to the model each turn


@dataclass
class Conversation:
    id: str
    visitor_key: str
    history: list[Message] = field(default_factory=list[Message])  # caller/assistant text only
    identity: IdentitySession = field(default_factory=IdentitySession)
    sched: SchedulingState = field(default_factory=SchedulingState)
    offered_slots: list[Slot] = field(default_factory=list[Slot])  # same order as sched.offered
    turn_no: int = 0  # caller messages so far; offers and read-backs must come from earlier turns
    offered_turn: int = -1  # turn in which offered_slots were shown to the caller
    readback: int | None = None  # option number read back to the caller, awaiting a yes
    readback_turn: int = -1
    pending_cancel: int | None = None  # visit id the caller was asked to confirm cancelling
    pending_cancel_turn: int = -1
    fee_asks: int = 0
    tried: list[str] = field(default_factory=list[str])  # for the handoff ticket (D11.5)
    sources: list[str] = field(default_factory=list[str])  # tool results so far (D19.4)
    closed: bool = False  # handed off: no more agent work this conversation
    callback_ticket: int | None = None  # waiting for the caller's preferred callback window


@dataclass
class Turn:
    """One caller message in, one reply out, plus what monitoring (T14) and checks (T11) need."""

    reply: str = ""
    route: str = ""  # safety | gate | kb | scheduling | handoff
    ticket: Ticket | None = None
    fee_button: str | None = None  # label of the consent button to show (D10.8)
    model_calls: list[Reply] = field(default_factory=list[Reply])
    tool_calls: list[str] = field(default_factory=list[str])
    sections: dict[str, str] = field(default_factory=dict[str, str])  # retrieved this turn
    citations: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    tool_results: list[str] = field(default_factory=list[str])  # DB facts returned this turn
    changes: list[str] = field(default_factory=list[str])  # bookings/moves/cancels made this turn
    check_failures: list[str] = field(default_factory=list[str])  # D19 codes, for monitoring
