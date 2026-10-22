"""T15 conversation eval: smoke run with fake models (R-26), sentinels (R-30), grading rules."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_fakes import INDEX, FakeLLM, r, route
from evals.conversations import build, run
from meridian.agent.graph import Agent
from meridian.agent.llm import Message, Tool, ToolCall, Usage
from meridian.db import connect


def _case(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "T-1",
        "bucket": "membership",
        "kind": "kb_question",
        "goal": "Find out what a tune-up costs.",
        "opening": "How much is a tune-up?",
        "caller_notes": "Not a member.",
        "accepts_fee": False,
        "facts_required": ["$129 for non-members"],
        "facts_forbidden": [],
        "db_facts": [],
        "service_type": None,
        "also_books": False,
        "should_handle": True,
        "expected_handoff": None,
        "customer": {
            "customer_id": "MHC-10001",
            "name": "Linda Foster",
            "phone": "512-555-0101",
            "address": "x",
            "tier": "standard",
            "appointment_id": 159,
        },
    }
    return base | kw


class FakeComplete:
    """Plays the caller ("[DONE]" at once) and the judge (a fixed verdict)."""

    def __init__(self, correct: bool = True, grounded: bool = True) -> None:
        self.verdict = {"correct": correct, "grounded": grounded, "why": "fake"}

    def __call__(
        self, model: str, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> tuple[str, tuple[ToolCall, ...], Usage]:
        if tools:
            return "", (ToolCall("v1", "verdict", dict(self.verdict)),), Usage()
        return "Thanks, that's all. [DONE]", (), Usage()


def _run(
    tmp_path: Path, cases: list[dict[str, Any]], llm: FakeLLM, complete: FakeComplete
) -> dict[str, Any] | None:
    def agent_for(db: Path) -> Agent:
        return Agent(lambda: connect(db), llm, lambda q: INDEX.search(q))  # type: ignore[arg-type]

    meta = {"label": "t", "split": "dev", "brain": "fake", "commit": "x"}
    return run.run(cases, tmp_path / "out", agent_for, complete, meta, workers=1)


def test_smoke_run_with_fake_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "SENTINELS", tmp_path / "none.jsonl")
    (tmp_path / "none.jsonl").write_text("")
    llm = FakeLLM()
    llm.add(  # the KB question, answered from a cited section
        route("kb"),
        r("", ("search_kb", {"query": "maintenance tune-up non-members included"})),
        r(
            "",
            (
                "answer",
                {
                    "text": "A maintenance tune-up is $129 for non-members.",
                    "citations": [
                        {
                            "section_id": "services/service_catalog.md#maintenance-tune-up-0",
                            "quote": "$129 for non-members",
                        }
                    ],
                },
            ),
        ),
    )
    cases = [
        _case(),
        _case(
            id="T-2",
            kind="handoff",
            should_handle=False,
            expected_handoff="safety_emergency",
            opening="I smell gas",
        ),
    ]
    s = _run(tmp_path, cases, llm, FakeComplete())
    assert s is not None and not llm.steps
    assert s["outcomes"] == {"contained": 1, "escalated_right_reason": 1}
    assert s["containment"] == 0.5 and s["grounded_rate"] == 1.0 and s["routing_accuracy"] == 1.0
    assert (tmp_path / "out/summary.md").read_text().startswith("# Conversation eval")
    # resumable: a second call re-runs nothing (no model steps left) and returns the summary
    assert _run(tmp_path, cases, llm, FakeComplete()) is not None


def test_a_sentinel_miss_marks_the_run_untrusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _run(tmp_path, [], FakeLLM(), FakeComplete(correct=True, grounded=True))
    assert s is not None and s["sentinel_misses"]  # "invented price" must be judged wrong
    assert "Untrusted" in run.render(s | {"cases": 0, "agent_cost_per_conversation_usd": 0})


def test_sentinels_cover_both_verdicts() -> None:
    rows = [json.loads(x) for x in run.SENTINELS.read_text().splitlines()]
    assert {(s["expect_correct"], s["expect_grounded"]) for s in rows} >= {
        (True, True),
        (False, False),
        (True, False),
    }


@pytest.mark.parametrize(
    ("kind", "after", "ok"),
    [
        ("book_visit", {1: (10, "scheduled", "x"), 2: (11, "scheduled", "hvac_repair")}, True),
        ("book_visit", {1: (10, "scheduled", "x")}, False),
        ("cancel", {1: (10, "cancelled", "x")}, True),
        ("reschedule", {1: (12, "scheduled", "x")}, True),
        ("kb_question", {1: (10, "cancelled", "x")}, False),
        ("kb_question", {1: (10, "scheduled", "x")}, True),
    ],
)
def test_db_check(kind: str, after: dict[int, Any], ok: bool) -> None:
    case = _case(kind=kind, service_type="hvac_repair" if kind == "book_visit" else None)
    case["customer"]["appointment_id"] = 1
    assert run.db_check(case, {1: (10, "scheduled", "x")}, after) is ok


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("book_visit", "cannot_be_booked_here"),
        ("reschedule", "cannot_be_booked_here"),
        ("cancel", "late_change_fee_declined"),
        ("reschedule", "late_change_fee_declined"),
    ],
)
def test_a_right_handoff_on_a_booking_case_counts_as_right(kind: str, reason: str) -> None:
    """T15 re-review: db_check expected the booking even when the case must hand off."""
    case = _case(kind=kind, should_handle=False, expected_handoff=reason)
    case["customer"]["appointment_id"] = 1
    same = {1: (10, "scheduled", "x")}
    assert run.db_check(case, same, same)
    assert not run.db_check(case, same, {1: (10, "cancelled", "x")})
    res = {"handoff": reason, "ending": "handoff", "db_ok": True, "fee_followup": False}
    assert run.grade(case, res, None)["outcome"] == "escalated_right_reason"


def test_the_24h_rule_is_exact() -> None:
    def visit(day: str, window: str) -> Any:
        return {"slot_date": day, "window": window}

    assert build.within_24h(visit("2026-10-17", "08:00-12:00"))  # 22h out
    assert not build.within_24h(visit("2026-10-17", "10:00-14:00"))  # exactly 24h
    assert build.within_24h(visit("2026-10-17", "09:59-14:00"))  # 23h59m


def test_grading_never_counts_a_handoff_or_a_give_up_as_contained() -> None:
    res = {"handoff": None, "ending": "gave_up", "db_ok": True, "fee_followup": False}
    assert run.grade(_case(), res, {"correct": True})["outcome"] == "abandoned"
    res |= {"handoff": "asked_for_a_person", "ending": "handoff"}
    assert run.grade(_case(), res, None)["outcome"] == "handed_off"
    res = {"handoff": None, "ending": "done", "db_ok": True, "fee_followup": False}
    assert run.grade(_case(), res, {"correct": False})["outcome"] == "wrong"
    must = _case(should_handle=False, expected_handoff="safety_emergency")
    assert run.grade(must, res, None)["outcome"] == "false_containment"


def test_splits_are_disjoint_and_sized() -> None:
    s = build.split(build.load_labels())
    assert (len(s["dev"]), len(s["heldout"]), len(s["final"])) == (60, 150, 360)
    assert not set(s["dev"]) & set(s["final"]) and set(s["heldout"]) <= set(s["final"])


def test_committed_cases_match_the_split() -> None:
    s = build.split(build.load_labels())
    for name in ("dev", "heldout", "final"):
        built = [c for c in run.load_cases(name) if c.get("source") != "feedback"]
        assert [c["id"] for c in built] == s[name]
        assert not any(c["customer"]["customer_id"].startswith("MHC-009") for c in built)
