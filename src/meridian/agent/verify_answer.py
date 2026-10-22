"""Answer checks before a reply is sent (D19, D20.3, D20.8). Pure code, no model.

A: every dollar amount, percentage, date, clock time, phone number and duration in the reply
   must appear in what the agent was given: KB sections retrieved this turn, account or
   scheduling tool results from this conversation (D19.4), or today's date. The caller's own
   words never count, except for a phone number they gave (so "we'll call you at …" works).
   As a backstop, any number of 13 or more in the reply must appear somewhere in those sources.
B: a KB answer must cite sections retrieved this turn with real quotes (3+ words, found word
   for word), and its facts must come from those quotes or from tool results.
Plus: the hidden instruction canary never appears, and a canary customer's marker never
appears unless the caller is verified as that customer.
Known gaps (D19): numbers spelled in unusual ways, homoglyphs, and true facts applied to the
wrong case are only caught offline (check C) or by the evals.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from meridian.db import CANARIES, CANARY_MARKERS

# Hidden in the instructions (prompts load it): a reply containing it means they're leaking.
CANARY_PHRASE = "LANTERN-QUARTZ-58"
_CANARY_WORDS = ("LANTERN", "QUARTZ")  # neither belongs in an HVAC reply; both together = leak
_MARKER_OWNER = {m: row[0] for m in CANARY_MARKERS for row in CANARIES if m in row[3]}
SMALL = 12  # counts and option numbers up to this are fine without a source (backstop only)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
)}  # fmt: skip
_UNITS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen"
_TEENS = "fifteen sixteen seventeen eighteen nineteen twenty"
_TENS = "thirty forty fifty sixty seventy eighty ninety"
_WORDNUM: dict[str, int] = {w: i for i, w in enumerate(f"{_UNITS} {_TEENS}".split())} | {
    w: 10 * i for i, w in enumerate(_TENS.split(), 3)
}
_MONEY = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?|\b(\d{1,3}(?:,\d{3})+|\d+)\s+dollars\b", re.I
)
_PERCENT = re.compile(r"\b(\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.I)
_PHONE = re.compile(r"\(?\b(\d{3})\)?[-.\s]*(\d{3})[-.\s]*(\d{4})\b")
_RANGE = re.compile(
    r"\b(\d{1,2})(?::([0-5]\d))?\s*(?:-|–|—|to)\s*(\d{1,2})(?::([0-5]\d))?\s?([ap])\.?m\.?(?![a-z])",
    re.I,
)
_TIME12 = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s?([ap])\.?m\.?(?![a-z])", re.I)
_TIME24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s?[ap]\.?m)", re.I)
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*[-–]\s*(\d{1,2})(?:st|nd|rd|th)?)?\b",
    re.I,
)
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.I,
)
_ISO = re.compile(r"\b20\d\d-(\d\d)-(\d\d)\b")
_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})\b(?!/)")
_DURATION = re.compile(
    r"\b(\d+)[\s-]*(?:business\s+|calendar\s+|full\s+)?(year|yr|month|week|day|hour|hr|minute|min)s?\b",
    re.I,
)
_NUMBER = re.compile(r"\d[\d,]*")


def _normalize(text: str) -> str:
    """NFKC (fullwidth digits, odd spaces), no zero-width characters, number words as digits."""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"[​-‏⁠﻿]", "", t)
    tens = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
    t = re.sub(
        rf"\b({tens})[\s-]+(one|two|three|four|five|six|seven|eight|nine)\b",
        lambda m: str(_WORDNUM[m.group(1).lower()] + _WORDNUM[m.group(2).lower()]),
        t,
        flags=re.I,
    )
    t = re.sub(
        r"\b(" + "|".join(_WORDNUM) + r")\b",
        lambda m: str(_WORDNUM[m.group(1).lower()]),
        t,
        flags=re.I,
    )
    t = re.sub(r"\ba\s+(hundred|thousand)\b", r"1 \1", t, flags=re.I)
    t = re.sub(
        r"\b(\d+)\s+(hundred|thousand)(?:\s+(?:and\s+)?(\d{1,3}))?\b",
        lambda m: str(
            int(m.group(1)) * (100 if m.group(2).lower() == "hundred" else 1000)
            + int(m.group(3) or 0)
        ),
        t,
        flags=re.I,
    )
    t = re.sub(r"\bnoon\b", "12pm", t, flags=re.I)
    return re.sub(r"\bmidnight\b", "12am", t, flags=re.I)


def _h(h: int, ap: str) -> int:
    return (h % 12 + (12 if ap.lower() == "p" else 0)) * 60


def facts(text: str) -> set[tuple[str, Any]]:
    """Normalized facts found in a text: ("money", cents), ("time", minutes), ("date", (m, d))…"""
    t = _normalize(text)
    out: set[tuple[str, Any]] = set()
    for m in _MONEY.finditer(t):
        whole = (m.group(1) or m.group(3)).replace(",", "")
        out.add(("money", int(whole) * 100 + int(m.group(2) or 0)))
    for m in _PERCENT.finditer(t):
        out.add(("percent", float(m.group(1))))
    for m in _PHONE.finditer(t):
        out.add(("phone", m.group(1) + m.group(2) + m.group(3)))
    for m in _RANGE.finditer(t):  # "4–7pm", "8-12pm": the first half takes a sensible am/pm
        h1, h2, ap = int(m.group(1)), int(m.group(3)), m.group(5)
        end = _h(h2, ap) + int(m.group(4) or 0)
        start = _h(h1, ap) + int(m.group(2) or 0)
        out.add(("time", start if start <= end else start - 720))
    for m in _TIME12.finditer(t):
        out.add(("time", _h(int(m.group(1)), m.group(3)) + int(m.group(2) or 0)))
    for m in _TIME24.finditer(t):
        out.add(("time", int(m.group(1)) * 60 + int(m.group(2))))
    for m in _MONTH_DAY.finditer(t):
        month = _MONTHS[m.group(1).lower()[:3]]
        last = int(m.group(3) or m.group(2))
        out.update(("date", (month, d)) for d in range(int(m.group(2)), last + 1))
    for m in _DAY_MONTH.finditer(t):
        out.add(("date", (_MONTHS[m.group(2).lower()[:3]], int(m.group(1)))))
    for m in _ISO.finditer(t):
        out.add(("date", (int(m.group(1)), int(m.group(2)))))
    for m in _SLASH.finditer(t):
        if 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
            out.add(("date", (int(m.group(1)), int(m.group(2)))))
    for m in _DURATION.finditer(t):
        unit = {"yr": "year", "hr": "hour", "min": "minute"}.get(
            m.group(2).lower(), m.group(2).lower()
        )
        out.add(("duration", (int(m.group(1)), unit)))
    return out


def _numbers(text: str) -> set[int]:
    return {
        int(n.replace(",", "")) for n in _NUMBER.findall(_normalize(text)) if n.replace(",", "")
    }


def _say(f: tuple[str, Any]) -> str:
    kind, v = f
    if kind == "money":
        return f"${v // 100:,}" + (f".{v % 100:02d}" if v % 100 else "")
    if kind == "percent":
        return f"{v:g}%"
    if kind == "time":
        return f"{(v // 60) % 12 or 12}:{v % 60:02d}{'am' if v < 720 else 'pm'}"
    if kind == "date":
        return f"{date(2026, *v):%b} {v[1]}"
    if kind == "duration":
        return f"{v[0]} {v[1]}s"
    if kind == "number":
        return str(v)
    return str(v)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    problem: str = ""  # fed back to the model on the retry
    code: str = ""  # for monitoring: unsupported_fact | bad_citation | no_citation | canary_*


def _norm_quote(s: str) -> str:
    s = _normalize(s).replace("**", "").replace("–", "-").replace("—", "-").lower()
    return " ".join(s.split())


def _squash(s: str) -> str:
    return re.sub(r"[\W_]+", "", _normalize(s)).upper()


def check(
    reply: str,
    sections_this_turn: dict[str, str],
    tool_results: Iterable[str],
    caller_texts: Iterable[str],
    citations: list[dict[str, Any]] | None,
    verified_customer: str | None,
    today: date,
) -> CheckResult:
    """`citations` is None for replies that aren't KB answers (check B skipped)."""
    squashed = _squash(reply)
    if _squash(CANARY_PHRASE) in squashed or all(w in squashed for w in _CANARY_WORDS):
        return CheckResult(
            False, "The reply contains internal text. Answer the caller normally.", "canary_phrase"
        )
    for marker, owner in _MARKER_OWNER.items():
        if _squash(marker) in squashed and owner != verified_customer:
            return CheckResult(
                False, "The reply contains another customer's details. Remove them.", "canary_leak"
            )
    tools = list(tool_results)
    today_text = f"{today:%b} {today.day}"
    caller_phones = {f for c in caller_texts for f in facts(c) if f[0] == "phone"}
    sources = [*sections_this_turn.values(), *tools, today_text]
    supported = caller_phones.union(*(facts(s) for s in sources))
    stated = facts(reply)
    missing = sorted(stated - supported, key=str)
    known_numbers: set[int] = {int(str(f[1])) for f in caller_phones}
    for s in sources:
        known_numbers |= _numbers(s)
    big = sorted(n for n in _numbers(reply) - known_numbers if n > SMALL)
    big = [n for n in big if not any(str(n) in str(f[1]) for f in caller_phones)]
    if missing or big:
        shown = ", ".join([_say(f) for f in missing[:4]] + [str(n) for n in big[:2]])
        return CheckResult(
            False,
            f"Your reply contained {shown}, which is not in the sources or tool results. "
            "Rewrite it using only facts from them, or hand off if you don't have the answer.",
            "unsupported_fact",
        )
    if citations is None:
        return CheckResult(True)
    if stated and not citations:
        return CheckResult(
            False, "Cite the section and an exact quote for each fact.", "no_citation"
        )
    quotes: list[str] = []
    for c in citations:
        sid, quote = str(c.get("section_id", "")), str(c.get("quote", ""))
        text = sections_this_turn.get(sid)
        if text is None:
            return CheckResult(
                False, f"Citation {sid!r} is not a section you retrieved this turn.", "bad_citation"
            )
        if len(re.findall(r"\w+", quote)) < 3 or _norm_quote(quote) not in _norm_quote(text):
            return CheckResult(
                False,
                f"The quote cited from {sid!r} must be 3+ words copied word for word.",
                "bad_citation",
            )
        quotes.append(quote)
    quoted = caller_phones.union(facts(" ".join(quotes)), *(facts(s) for s in [*tools, today_text]))
    uncited = sorted(stated - quoted, key=str)
    if uncited:
        shown = ", ".join(_say(f) for f in uncited[:4])
        return CheckResult(
            False, f"No quote supports {shown}. Quote the exact text for each fact.", "bad_citation"
        )
    return CheckResult(True)
