"""Handoffs (D11): code, not a model. Builds the caller message (what / why / next); the ticket
is saved by `monitoring.records` (DB writes stay out of agent/, rule 4)."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Urgency = Literal["emergency", "urgent", "standard"]


class Reason(StrEnum):
    SAFETY = "safety_emergency"
    CRISIS = "caller_in_crisis"
    URGENT = "urgent_no_heat_no_cool_or_leak"
    HUMAN_REQUEST = "asked_for_a_person"
    SUPERVISOR = "supervisor_callback"  # dispute, refund, complaint, manager, legal/chargeback
    NOT_ALLOWED = "not_allowed_for_the_assistant"  # waivers, discounts, new customers, payments…
    SCHEDULING_GAP = "cannot_be_booked_here"  # same day, no zone, no slots for the service
    IDENTITY_FAILED = "identity_not_verified"
    NO_SOURCE = "no_supporting_source"
    CHECK_FAILED = "answer_check_failed"
    FEE_DECLINED = "late_change_fee_declined"
    MODEL_DOWN = "assistant_unavailable"
    OUT_OF_SCOPE = "out_of_scope"
    NOT_WORKING = "conversation_not_working"


URGENCY: dict[Reason, Urgency] = {
    Reason.SAFETY: "emergency",
    Reason.CRISIS: "urgent",
    Reason.URGENT: "urgent",
}

# Why, in plain words and without blame (D11.4). "What" and "next" are added by urgency.
_WHY: dict[Reason, str] = {
    Reason.SAFETY: "",
    Reason.CRISIS: "",
    Reason.URGENT: "This needs a technician as soon as possible, so I'm connecting you now.",
    Reason.HUMAN_REQUEST: "Of course, I'll get you to a person.",
    Reason.SUPERVISOR: "This is something a member of our team needs to review personally.",
    Reason.NOT_ALLOWED: "That's something a member of our team has to handle directly.",
    Reason.SCHEDULING_GAP: "I can't book that one here, but our team can arrange it with you.",
    Reason.IDENTITY_FAILED: "I wasn't able to verify the account here, so a team member will help.",
    Reason.NO_SOURCE: "I don't have a reliable answer to that, and I'd rather not guess.",
    Reason.CHECK_FAILED: "I want you to get an accurate answer, so a team member will follow up.",
    Reason.FEE_DECLINED: "No problem, I've left your visit as it was.",
    Reason.MODEL_DOWN: "I'm having trouble on my end, so let me get you to a person.",
    Reason.OUT_OF_SCOPE: "That's outside what I can help with here, but our team can.",
    Reason.NOT_WORKING: "I don't think I'm getting this right, so let me get you to a person.",
}
_NEXT: dict[Urgency, str] = {
    "emergency": "",
    "urgent": "Someone from our team will contact you right away.",
    "standard": (
        "A team member will call you back within one business day. "
        "What's the best time to reach you?"
    ),
}


@dataclass(frozen=True)
class Ticket:
    id: int
    reason: Reason
    urgency: Urgency
    customer_id: str | None
    verified: bool
    summary: str
    tried: str
    wants: str


def caller_message(reason: Reason, script: str = "") -> str:
    """What the caller hears. Safety and crisis use their fixed script (passed in)."""
    urgency = URGENCY.get(reason, "standard")
    parts = [script or _WHY[reason], _NEXT[urgency]]
    return " ".join(p for p in parts if p)


def asks_for_callback_window(reason: Reason) -> bool:
    return URGENCY.get(reason, "standard") == "standard"
