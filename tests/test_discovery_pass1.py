from pathlib import Path

import numpy as np
import pytest

from discovery.buckets import cell, kmeans, validate_proposal
from discovery.common import append_cache, load_cache, parse_json_object
from discovery.pass1 import validate


def test_parse_json_object_handles_fences_and_rejects_garbage() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("no json here")


def test_cache_last_row_wins_and_skips_truncated_lines(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    append_cache(p, {"call_id": "A", "reason": "old", "prompt_version": "v1"})
    append_cache(p, {"call_id": "A", "reason": "new", "prompt_version": "v2"})
    append_cache(p, {"call_id": "B", "reason": "y", "prompt_version": "v2"})
    with p.open("a") as f:
        f.write('{"call_id": "C", "rea')  # write cut off mid-line
    rows = load_cache(p)
    assert set(rows) == {"A", "B"} and rows["A"]["reason"] == "new"
    assert set(load_cache(p, "v1")) == {"A"}
    assert load_cache(tmp_path / "missing.jsonl") == {}


def test_validate_accepts_good_and_rejects_bad_outcome() -> None:
    good = {
        "reason": " AC warm ",
        "second_reason": "",
        "outcome": "escalated",
        "account_lookup": True,
    }
    assert validate(good) == {
        "reason": "AC warm",
        "second_reason": None,
        "outcome": "escalated",
        "account_lookup": True,
    }
    with pytest.raises(ValueError):
        validate({**good, "outcome": "booked"})
    with pytest.raises(ValueError):
        validate({**good, "reason": "  "})


def test_kmeans_is_deterministic_and_separates_obvious_groups() -> None:
    x = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9]])
    a, b = kmeans(x, 2), kmeans(x, 2)
    assert list(a) == list(b)
    assert a[0] == a[1] and a[2] == a[3] and a[0] != a[2]


def _bucket(bid: str, examples: list[str]) -> dict[str, object]:
    return {
        "id": bid,
        "name": bid,
        "definition": "d",
        "includes": "i",
        "excludes": "e",
        "examples": examples,
        "estimated_count": 3,
    }


def test_validate_proposal_drops_invented_ids_and_rejects_bad_shapes() -> None:
    known = {"C1", "C2"}
    ok = validate_proposal({"buckets": [_bucket("a", ["C1", "FAKE"]), _bucket("b", ["C2"])]}, known)
    assert ok["buckets"][0]["examples"] == ["C1"]
    with pytest.raises(ValueError, match="no real example"):
        validate_proposal({"buckets": [_bucket("a", ["FAKE"]), _bucket("b", ["C2"])]}, known)
    with pytest.raises(ValueError, match="duplicate"):
        validate_proposal({"buckets": [_bucket("a", ["C1"]), _bucket("a", ["C2"])]}, known)
    with pytest.raises(ValueError):
        validate_proposal({"buckets": [_bucket(f"b{i}", ["C1"]) for i in range(11)]}, known)
    with pytest.raises(ValueError):
        validate_proposal({"nothing": []}, known)


def test_cell_escapes_table_pipes_and_newlines() -> None:
    assert cell("a | b\nc") == "a \\| b c"
