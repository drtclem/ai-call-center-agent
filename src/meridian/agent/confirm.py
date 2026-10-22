"""Is the caller agreeing to a specific offered slot? Decided in code (D10.6).

Real replies from the transcripts ("That's fine.", "Sure, let's do that.", "Yeah, that works.",
"Works.") agree; anything that also asks for a change ("sure, but can it be earlier?",
"yes... wait, Wednesday") or isn't an answer ("can you hear me?") does not.
"""

import re
from enum import StrEnum

from meridian.scheduling import Slot


class Reply(StrEnum):
    AGREES = "agrees"
    DECLINES = "declines"
    DIFFERENT = "different"
    UNCLEAR = "unclear"


_AGREE = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|fine|works|perfect|great|sounds\s+good|"
    r"let'?s\s+do\s+(?:that|it|the)|book\s+(?:it|that|me)|go\s+ahead|please\s+do|"
    r"i'?ll\s+take|that\s+one|correct|absolutely|definitely)\b",
    re.I,
)
_CHANGE = re.compile(
    r"\b(?:but|wait|actually|earlier|later|sooner|instead|different|other|another|"
    r"hold\s+on|hmm+|unless|maybe|not\s+sure|rather)\b|\?",
    re.I,
)
# Any negation anywhere means "not a clear yes" ("that's not fine", "neither works", "Tuesday
# isn't great").
_DECLINE = re.compile(
    r"^\s*(?:nope|nah|never\s*mind)\b|\bno\b(?!\s+(?:problem|prob|worries|rush|preference))|"
    r"\b(?:not|isn'?t|aren'?t|neither|nor|nothing|none|"
    r"doesn'?t|don'?t|won'?t|can'?t|cannot|wouldn'?t)\b",
    re.I,
)
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
# Whole words only ("my friend" is not Friday). Short names stay: a named day that fits no
# offer must block the read-back, not vanish ("yes, Wed works" with Monday read back).
_DAY_WORDS = [
    r"(?<!c')\bmon(?:day)?\b",  # not "c'mon"
    r"\btues?(?:day)?\b",
    r"\bweds?(?:nesday)?\b",
    r"\bthu(?:rs?)?(?:day)?\b",
    r"\bfri(?:day)?\b",
    r"\bsat(?:urday)?\b",
    r"\bsun(?:day)?\b",
]


_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_TIME = re.compile(
    r"\b\d{1,2}(?::\d\d)?\s*(?:am|pm)\b|\bnoon\b|\b\d{1,2}\s*(?:to|-|–|until)\s*\d{1,2}\b"
)


def _h12(hhmm: str) -> tuple[int, str]:
    h = int(hhmm[:2])
    return (h % 12 or 12), ("am" if h < 12 else "pm")


def _window_said(t: str, window: str) -> bool:
    (a, a_m), (b, b_m) = (_h12(x) for x in window.split("-"))
    start = rf"(?:{a}(?::00)?\s*(?:{a_m})?" + (r"|noon" if a == 12 else "") + ")"
    end = rf"{b}(?::00)?\s*(?:{b_m})?"
    return bool(
        re.search(rf"\b{start}\s*(?:to|-|–|until)\s*{end}\b", t)
        or re.search(rf"\b(?:{a}(?::00)?\s*{a_m}" + (r"|noon" if a == 12 else "") + r")\b", t)
    )


def _given_name(name: str) -> str:
    """ "Tasha Bell" -> "Tasha"; "Comfort Advisor - Lee" -> "Lee"."""
    return name.split(" - ")[-1] if " - " in name else name.split()[0]


def picked(text: str, offered: list[Slot]) -> int | None:
    """1-based option the caller named, or None if they named none or their clues disagree."""
    left = candidates(text, offered)
    return left.pop() if left is not None and len(left) == 1 else None


def candidates(text: str, offered: list[Slot]) -> set[int] | None:
    """The options the caller's words fit (None: they gave no clue at all).

    Every clue narrows the options: an option number or ordinal ("option 2", "the second"), a
    weekday, a date ("the 19th", "October 19"), a time window ("12 to 4", "8am") and the
    technician's name. A clue that fits no offered option (a time we never offered) means the
    caller isn't picking one of these."""
    t = text.lower().replace("’", "'")
    every = set(range(1, len(offered) + 1))
    clues: list[set[int]] = []
    m = re.search(r"\b(?:option|number|#)\s*([1-3])\b", t)
    if m:
        clues.append({int(m.group(1))})
    for word in re.findall(r"\b(first|1st|second|2nd|third|3rd)\b", t):
        if not re.search(rf"\b{word}\s+(?:of|week)\b", t):  # "the 3rd of", "first week"
            clues.append({_ORDINALS[word]})
    days = {i for i, s in enumerate(offered, 1) if re.search(_DAY_WORDS[s.date.weekday()], t)}
    if days or any(re.search(p, t) for p in _DAY_WORDS):
        clues.append(days)  # naming a day we didn't offer is not agreeing to one we did
    dates = {
        i
        for i, s in enumerate(offered, 1)
        if re.search(
            rf"(?:\bthe\s+|{_MONTHS}\s+){s.date.day}(?:st|nd|rd|th)?\b|\b{s.date.day}(?:st|nd|rd|th)\b",
            t,
        )
    }
    if dates:
        clues.append(dates)
    if _TIME.search(t):
        clues.append({i for i, s in enumerate(offered, 1) if _window_said(t, s.window)})
    # given names only: surnames like Park or Bell are everyday words ("park in the driveway")
    techs = {
        i
        for i, s in enumerate(offered, 1)
        if re.search(
            rf"(?<![\w-])(?<!x\s){re.escape(_given_name(s.tech_name).lower())}\b", t
        )  # not x-ray
    }
    if techs:
        clues.append(techs)
    return every.intersection(*clues) if clues else None


_WAITLIST = re.compile(r"[^.!?]*\b(?:wait\s*-?\s*list|cancellation\s+list)\b[^.!?]*[.!?]?", re.I)
# Inside a waitlist sentence only "earlier/later/another time" and a question are expected; any
# other change or decline word ("no", "actually", "instead") keeps the sentence in play.
_WAITLIST_OK = re.compile(r"\b(?:earlier|later|sooner|other|another|different)\b|\?", re.I)


def _without_waitlist(text: str) -> str:
    """Drop a plain waitlist ask ("Yes, book Friday. And add me to the waitlist for something
    earlier?"): it's a separate request (the assistant can't add anyone to one; the worker says
    so)."""

    def drop(m: re.Match[str]) -> str:
        rest = _WAITLIST_OK.sub(
            " ", re.sub(r"wait\s*-?\s*list", "waitlist", m.group(0), flags=re.I)
        )
        return m.group(0) if _CHANGE.search(rest) or _DECLINE.search(rest) else " "

    return _WAITLIST.sub(drop, text)


def classify(text: str) -> Reply:
    text = _without_waitlist(text)
    if _CHANGE.search(text):
        return Reply.DIFFERENT
    if _DECLINE.search(text):
        return Reply.DECLINES
    if _AGREE.search(text):
        return Reply.AGREES
    return Reply.UNCLEAR


def agreed_option(text: str, offered: list[Slot], readback: int | None) -> int | None:
    """The option the caller clearly agreed to, or None. A bare "yes" counts only when one slot
    is on the table: a single offer, or the slot just read back to them."""
    if classify(text) is not Reply.AGREES:
        return None
    text = _without_waitlist(text)
    if re.search(r"\bor\b", text, re.I):  # "yes, monday, or tuesday": still choosing
        return None
    left = candidates(text, offered)
    if left is None:  # a bare "yes"
        return readback if readback is not None else (1 if len(offered) == 1 else None)
    if len(left) == 1:
        return left.pop()
    return readback if readback in left else None  # "yes, Monday" fits the one read back


def describe(s: Slot) -> str:
    return f"{s.date:%a %b} {s.date.day}, {window_text(s.window)}"


def window_text(window: str) -> str:
    start, end = window.split("-")
    return f"{_hour(start)}–{_hour(end)}"


def _hour(hhmm: str) -> str:
    h = int(hhmm[:2])
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"
