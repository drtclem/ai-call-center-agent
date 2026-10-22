"""Shared helpers for the offline discovery pipeline (D2)."""

import csv
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

DATA = Path("data/meridian_dataset")
TRANSCRIPTS = DATA / "call_transcripts"
INDEX = DATA / "call_transcripts_index.csv"
CACHE = Path("discovery/cache")


def call_ids() -> list[str]:
    """All call IDs from the index, in index order."""
    with INDEX.open() as f:
        return [row["call_id"] for row in csv.DictReader(f)]


def transcript(call_id: str) -> str:
    return (TRANSCRIPTS / f"{call_id}.txt").read_text()


def load_cache(path: Path, prompt_version: str | None = None) -> dict[str, dict[str, Any]]:
    """JSONL cache keyed by call_id (last row wins). Missing file → empty.

    Unparseable lines (e.g. a write cut off mid-line) are skipped so resume still works.
    With `prompt_version`, rows from other prompt versions count as missing.
    """
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if prompt_version is None or row.get("prompt_version") == prompt_version:
            out[row["call_id"]] = row
    return out


def append_cache(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first {...} block from a model reply (models sometimes add a fence)."""
    m = _JSON_RE.search(text)
    if m is None:
        raise ValueError("no JSON object in reply")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("reply JSON is not an object")
    return obj  # pyright: ignore[reportUnknownVariableType] -- validated by callers


def with_retries[T](fn: Callable[[], T], tries: int = 3) -> T:
    """Retry a parse-or-call step a few times; re-raise the last error."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            return fn()
        except (ValueError, KeyError) as exc:
            last = exc
    assert last is not None
    raise last
