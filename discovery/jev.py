"""Minimal Jev client for fixed-choice labeling (D2, labeler 2), via OpenRouter.

Jev is Typesafe's model; the owner can only pay approved vendors, so it is reached through
OpenRouter's decisions endpoint, which takes Typesafe's request format (state + "choice"
questions with criteria) and returns probabilities. Verified live 2026-10-19.
Model pinned to jev-1.14; the dated version that answered is recorded per label.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.14"
KEY_ENV = "OPENROUTER_API_KEY"
QUESTION_ID = "bucket"
_RETRY_STATUS = {429, 529}


@dataclass(frozen=True)
class Choice:
    label: str
    probabilities: dict[str, float]
    confidence: float
    model: str


def build_request(state: str, instructions: str, criteria: dict[str, str | None]) -> dict[str, Any]:
    """One Choice question over one transcript. Option keys are the bucket labels."""
    if not 2 <= len(criteria) <= 255:
        raise ValueError("Jev choice questions need 2-255 options")
    return {
        "state": state,
        "model": JEV_MODEL,
        "questions": {
            QUESTION_ID: {"type": "choice", "instructions": instructions, "criteria": criteria}
        },
    }


def parse_response(body: dict[str, Any], labels: set[str]) -> Choice:
    """Validate the answer; any malformed body becomes one ValueError."""
    try:
        answer = body["answers"][QUESTION_ID]
        choice = Choice(
            label=str(answer["choice"]),
            probabilities={str(k): float(v) for k, v in answer["probabilities"].items()},
            confidence=float(answer["confidence"]),
            model=str(body.get("model", "")),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("bad Jev response") from exc
    if choice.label not in labels or not set(choice.probabilities) <= labels:
        raise ValueError("bad Jev response: unknown label")
    return choice


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        after = resp.headers.get("retry-after")
        if after and after.isdigit():
            return float(after)
    return float(2**attempt)


def classify(
    client: httpx.Client,
    state: str,
    instructions: str,
    criteria: dict[str, str | None],
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Choice:
    """Call Jev with a timeout; back off on 429/529/5xx and network errors. Never logs the key."""
    assert retries >= 0
    key = os.environ.get(KEY_ENV)
    if not key:
        raise RuntimeError(f"{KEY_ENV} is not set")
    payload = build_request(state, instructions, criteria)
    for attempt in range(retries + 1):
        try:
            resp = client.post(
                JEV_URL, json=payload, headers={"Authorization": f"Bearer {key}"}, timeout=30.0
            )
        except httpx.TimeoutException, httpx.TransportError:
            if attempt == retries:
                raise
            sleep(_retry_delay(None, attempt))
            continue
        if resp.status_code in _RETRY_STATUS or resp.status_code >= 500:
            if attempt == retries:
                raise RuntimeError(f"Jev gave up after {retries + 1} tries: {resp.status_code}")
            sleep(_retry_delay(resp, attempt))
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Jev error {resp.status_code}: {resp.text[:300]}")
        return parse_response(resp.json(), set(criteria))
    raise AssertionError("unreachable")
