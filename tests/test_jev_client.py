from collections.abc import Callable
from typing import Any

import httpx
import pytest

from discovery.jev import build_request, classify, parse_response

CRITERIA: dict[str, str | None] = {"a": "first", "b": None}


def _ok(choice: str = "a") -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "bucket": {"choice": choice, "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.8}
        },
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _status(
    code: int, body: dict[str, Any] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json=body)

    return handler


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def test_build_request_shapes_a_choice_question() -> None:
    q = build_request("call text", "Why?", CRITERIA)["questions"]["bucket"]
    assert q == {"type": "choice", "instructions": "Why?", "criteria": CRITERIA}


def test_build_request_rejects_too_few_options() -> None:
    with pytest.raises(ValueError):
        build_request("x", "Why?", {"only": None})


def test_parse_response_reads_choice() -> None:
    c = parse_response(_ok(), {"a", "b"})
    assert (c.label, c.confidence, c.model) == ("a", 0.8, "jev-1.13.0")


def test_parse_response_rejects_unknown_label_and_malformed_body() -> None:
    with pytest.raises(ValueError):
        parse_response(_ok("zzz"), {"a", "b"})
    with pytest.raises(ValueError):
        parse_response({"answers": {}}, {"a", "b"})


def test_classify_sends_bearer_key_and_returns_choice() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["authorization"])
        return httpx.Response(200, json=_ok())

    c = classify(_client(handler), "t", "Why?", CRITERIA, sleep=lambda _s: None)
    assert c.label == "a" and seen == ["Bearer test-key"]


def test_classify_retries_429_honoring_retry_after_then_succeeds() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, json=_ok())

    classify(_client(handler), "t", "Why?", CRITERIA, sleep=slept.append)
    assert calls["n"] == 2 and slept == [7.0]


def test_classify_retries_network_errors_then_gives_up() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    slept: list[float] = []
    with pytest.raises(httpx.ConnectError):
        classify(_client(handler), "t", "Why?", CRITERIA, retries=2, sleep=slept.append)
    assert slept == [1.0, 2.0]


def test_classify_gives_up_on_repeated_5xx() -> None:
    with pytest.raises(RuntimeError, match="gave up"):
        classify(
            _client(_status(503)),
            "t",
            "Why?",
            CRITERIA,
            retries=1,
            sleep=lambda _s: None,
        )


def test_classify_reports_4xx_body_without_retry() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="criteria must be a map")

    with pytest.raises(RuntimeError, match="422: criteria must be a map"):
        classify(_client(handler), "t", "Why?", CRITERIA, sleep=lambda _s: None)


def test_classify_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(RuntimeError, match="not set"):
        classify(_client(_status(200, _ok())), "t", "Why?", CRITERIA)
