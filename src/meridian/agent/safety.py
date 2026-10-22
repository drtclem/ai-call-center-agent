"""Checks that run in code on every caller message, before any model call (D8.2, D11, D20.5).

Deliberately conservative: a false alarm costs a callback; a missed gas leak costs much more.
A short negation right before a phrase ("no gas smell", "I don't smell gas") does not trigger.
"""

import re
from enum import StrEnum

# Meridian's protocol (policies/escalation_and_safety.md), said word for word (D11).
SAFETY_SCRIPT = (
    "Please leave the home now and call 911 or your gas company from outside. "
    "Don't flip any switches or restart any equipment on the way out. "
    "I've flagged this as a priority emergency so our team follows up with you."
)
CRISIS_REPLY = (
    "I'm really sorry you're going through this. You don't have to handle it alone: you can "
    "call or text 988 (the Suicide & Crisis Lifeline) any time to talk with someone right now. "
    "If you're in immediate danger, please call 911. I'm also asking a member of our team to "
    "reach out to you."
)


class Alarm(StrEnum):
    SAFETY = "safety"
    CRISIS = "crisis"
    HUMAN = "human_request"
    MANAGER = "manager_request"


_SMELL = r"(?:smell\w*|odou?r\w*|stink\w*|reek\w*)"
_SAFETY = [
    rf"{_SMELL}\s+(?:of\s+|like\s+)?(?:natural\s+|propane\s+)?(?:gas|propane)\b",
    rf"\b(?:gas|propane)\s+(?:{_SMELL}|leak\w*)",
    r"rotten[\s-]+eggs?",
    r"\bsul(?:f|ph)ur\b",
    r"\bcarbon\s+monoxide\b",
    r"\bco\s+(?:alarm|detector|monitor)s?\s+(?:is\s+|are\s+|keeps\s+)?(?:going\s+off|went\s+off|"
    r"beep\w*|alarm\w*|sound\w*|chirp\w*|read\w*|says|show\w*|\d)",
    r"\bspark(?:s|ing|ed)?\b",
    r"\bsmok(?:e|ing|y)\b(?!\s+(?:detector|alarm)s?\b)",
    r"\bsmoke\s+(?:detector|alarm)s?\s+(?:is\s+|are\s+|keeps\s+)?(?:going\s+off|went\s+off|alarm\w*|sound\w*)",
    r"\bflames?\b",
    r"\bfire\b",
    rf"\bburn(?:ing|t)\b[^.?!]{{0,40}}\b{_SMELL}",
    rf"\b{_SMELL}\b[^.?!]{{0,30}}\bburn(?:ing|t)\b",
    rf"\belectrical\s+{_SMELL}|\b{_SMELL}\s+(?:\w+\s+){{0,2}}electrical\b",
]
_CRISIS = [
    r"\bkill\s+myself\b",
    r"\bsuicid\w*",
    r"\bend\s+(?:it\s+all|my\s+life)\b",
    r"\bwant\s+to\s+die\b",
    r"\bhurt\s+myself\b",
    r"\bself[\s-]+harm\b",
    # T16 red team: "I don't want to live anymore" reached the supervisor as "urgent". These
    # end at the clause or need "anymore", so "I don't want to live without AC another day",
    # "I don't want to go on the waitlist" and "take my life savings" don't trigger.
    r"\b(?:don'?t|do\s+not|no\s+longer)\s+(?:want\s+to|wanna)\s+"
    r"(?:live|be\s+alive|be\s+here|go\s+on|exist)(?:\s+like\s+this)?\s+any\s*more\b",
    r"\b(?:don'?t|do\s+not|no\s+longer)\s+(?:want\s+to|wanna)\s+(?:live|be\s+alive)"
    r"(?:\s+like\s+this)?\s*(?:[.!?,;]|$)",
    r"\bno\s+(?:reason|point)\s+(?:to|in)\s+(?:live|living|go(?:ing)?\s+on)\s*(?:any\s*more)?"
    r"\s*(?:[.!?,;]|$)",
    r"\bcan'?t\s+go\s+on\s+any\s*more\b",
    r"\bwish\s+i\s+(?:was|were)\s+dead\b",
    r"\bnot\s+worth\s+living\b",
    r"\bbetter\s+off\s+dead\b",
    r"\btake\s+my\s+(?:own\s+)?life\s*(?:[.!?,;]|$)",
]
_HUMAN = [
    r"\b(?:talk|speak|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|someone\s+|somebody\s+)?"
    r"(?:real\s+|live\s+|actual\s+)?(?:human|person|people|representative|rep|agent|operator|"
    r"someone|somebody)\b",
    r"\b(?:transfer|connect)\s+me\b",
    r"\b(?:real|live|actual)\s+(?:human|person|agent)\b",
    r"^\s*(?:human|representative|operator|agent)\s*(?:please)?[.!]*\s*$",
]
_MANAGER = [
    r"\b(?:speak|talk|get|want|need|see|ask\s+for|let\s+me\s+(?:speak|talk))\b[^.?!]{0,25}"
    r"\b(?:your\s+|a\s+|the\s+)?(?:manager|supervisor)\b",
    r"^\s*(?:manager|supervisor)\s*(?:please)?[.!]*\s*$",
]
# A negation directly before the phrase, with at most a filler word between ("no gas smell",
# "I don't smell gas", "not any smoke"). It never crosses a comma or clause.
_NEGATION = re.compile(
    r"\b(?:no|not|don't|doesn't|didn't|isn't|never|without)\s+(?:(?:any|a|really|even)\s+)?$"
)


# "Am I talking to a real person or a bot?" asks what we are; it isn't a request for a person
# (T16 red team). The assistant answers that it's an AI (D20.2).
_ASKS_IF_HUMAN = re.compile(
    r"^\s*(?:am\s+i|are\s+you|is\s+(?:this|that|it))\b[^.!?]*\?\s*(?:be\s+honest[.!]?)?\s*$",
    re.IGNORECASE,
)


# "Is it possible to talk to a real person?" is a request, not a question about us (D11)
_REQUEST_VERB = re.compile(r"\b(?:talk|speak|chat|connect|transfer|put\s+me)\b", re.IGNORECASE)


def _hits(patterns: list[str], text: str, negatable: bool) -> bool:
    for p in patterns:
        for m in re.finditer(p, text, re.IGNORECASE):
            if negatable and _NEGATION.search(text[max(0, m.start() - 40) : m.start()].lower()):
                continue
            return True
    return False


def check(text: str) -> Alarm | None:
    """The first alarm that applies, most serious first."""
    text = text.replace("’", "'").replace("‘", "'")
    if _hits(_SAFETY, text, negatable=True):
        return Alarm.SAFETY
    if _hits(_CRISIS, text, negatable=False):
        return Alarm.CRISIS
    if _hits(_MANAGER, text, negatable=True):
        return Alarm.MANAGER
    if _hits(_HUMAN, text, negatable=True) and not (
        _ASKS_IF_HUMAN.search(text) and not _REQUEST_VERB.search(text)
    ):
        return Alarm.HUMAN
    return None
