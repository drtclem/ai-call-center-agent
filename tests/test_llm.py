"""T9 model adapter with fakes (D7; R-24, R-25). No network."""

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from meridian.agent.llm import (
    CHAT_TIMEOUT_S,
    LLM,
    AnthropicProvider,
    Message,
    ModelUnavailable,
    OpenAIProvider,
    Tool,
    ToolCall,
    Truncated,
    Usage,
    cost_usd_micros,
)


class Scripted:
    """A provider that replays outcomes: an Exception to raise, or a (text, calls, usage)."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.models: list[str] = []

    def complete(
        self, model: str, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> tuple[str, tuple[ToolCall, ...], Usage]:
        self.models.append(model)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


OK = ("hello", (), Usage(1000, 200))
HI = [Message("user", "hi")]


def test_primary_answers() -> None:
    p, b = Scripted(OK), Scripted()
    r = LLM(p, b).chat("brain", "sys", HI)
    assert (r.text, r.model, r.used_backup, r.cost_usd_micros) == (
        "hello",
        "claude-sonnet-5-5",
        False,
        4000,
    )
    assert b.models == []


def test_one_retry_on_the_primary_then_success() -> None:
    p = Scripted(TimeoutError(), OK)
    r = LLM(p, Scripted()).chat("supervisor", "sys", HI)
    assert p.models == ["claude-haiku-5-5"] * 2 and not r.used_backup


def test_backup_after_two_primary_failures() -> None:
    p, b = Scripted(TimeoutError(), RuntimeError("429")), Scripted(OK)
    r = LLM(p, b).chat("brain", "sys", HI)
    assert r.used_backup and r.model == "gpt-6-astra" and r.cost_usd_micros == 1000 * 10 + 200 * 50


def test_everything_down_raises_for_a_polite_handoff() -> None:
    with pytest.raises(ModelUnavailable):
        LLM(Scripted(TimeoutError(), TimeoutError()), Scripted(ConnectionError())).chat(
            "brain", "s", HI
        )


def test_costs_use_cache_prices_and_haiku_long_prompt_tier() -> None:
    assert (
        cost_usd_micros("claude-sonnet-5-5", Usage(100, 10, 1000, 200))
        == 100 * 2 + 10 * 10 + 100 + 500
    )
    assert cost_usd_micros("claude-haiku-5-5", Usage(1000, 1000)) == 600  # 0.10 + 0.50 per token
    assert cost_usd_micros("claude-haiku-5-5", Usage(100_001, 0)) == 50_001  # above 100k: 0.50
    assert cost_usd_micros("claude-haiku-5-5", Usage(1, 0)) == 1  # fractions round up, never 0


TOOLS = [Tool("search_kb", "Search", {"type": "object", "properties": {"q": {"type": "string"}}})]
CONVO = [
    Message("user", "price?"),
    Message(
        "assistant",
        "",
        (ToolCall("t1", "search_kb", {"q": "a"}), ToolCall("t2", "search_kb", {"q": "b"})),
    ),
    Message("tool", "A", tool_call_id="t1"),
    Message("tool", "B", tool_call_id="t2"),
]


def test_anthropic_request_shape_and_reply_parsing() -> None:
    seen: dict[str, Any] = {}

    def create(**kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="text", text="Let me check."),
                SimpleNamespace(type="tool_use", id="t3", name="search_kb", input={"q": "c"}),
            ],
            usage=SimpleNamespace(
                input_tokens=5,
                output_tokens=7,
                cache_read_input_tokens=11,
                cache_creation_input_tokens=None,
            ),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    text, calls, usage = AnthropicProvider(client).complete("m", "sys", CONVO, TOOLS)  # type: ignore[arg-type]
    assert (text, calls, usage) == (
        "Let me check.",
        (ToolCall("t3", "search_kb", {"q": "c"}),),
        Usage(5, 7, 11, 0),
    )
    msgs = seen["messages"]
    assert [m["role"] for m in msgs] == [
        "user",
        "assistant",
        "user",
    ]  # both tool results in one turn
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["t1", "t2"]
    assert all(set(m) == {"role", "content"} for m in msgs)
    assert seen["timeout"] == CHAT_TIMEOUT_S and seen["system"] == "sys"
    assert seen["tools"][0]["input_schema"] == TOOLS[0].parameters


def test_openai_request_shape_and_reply_parsing() -> None:
    seen: dict[str, Any] = {}

    def create(**kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call", call_id="c9", name="search_kb", arguments='{"q": "d"}'
                )
            ],
            output_text="",
            status="completed",
            usage=SimpleNamespace(
                input_tokens=50,
                output_tokens=5,
                input_tokens_details=SimpleNamespace(cached_tokens=20),
            ),
        )

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    text, calls, usage = OpenAIProvider(client).complete("m", "sys", CONVO, TOOLS)  # type: ignore[arg-type]
    assert (text, calls, usage) == (
        "",
        (ToolCall("c9", "search_kb", {"q": "d"}),),
        Usage(30, 5, 20, 0),
    )
    types = [i.get("type", i.get("role")) for i in seen["input"]]
    assert types == [
        "user",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert (
        seen["store"] is False
        and seen["instructions"] == "sys"
        and seen["timeout"] == CHAT_TIMEOUT_S
    )


def test_malformed_tool_arguments_count_as_a_failure() -> None:
    def create(**_: Any) -> Any:
        return SimpleNamespace(
            output=[
                SimpleNamespace(type="function_call", call_id="c", name="x", arguments="{not json")
            ],
            output_text="",
            usage=None,
        )

    bad = OpenAIProvider(SimpleNamespace(responses=SimpleNamespace(create=create)))  # type: ignore[arg-type]
    with pytest.raises(ModelUnavailable):
        LLM(Scripted(TimeoutError(), TimeoutError()), bad).chat("brain", "s", HI)


def _anthropic_auth_error() -> Exception:
    import anthropic

    class Auth(anthropic.AuthenticationError):  # skips the SDK's HTTP-response constructor
        def __init__(self) -> None:
            Exception.__init__(self, "bad key")

    return Auth()


def test_errors_that_cannot_succeed_skip_the_retry() -> None:
    p, b = Scripted(_anthropic_auth_error()), Scripted(OK)
    r = LLM(p, b).chat("brain", "s", HI)
    assert r.used_backup and p.models == ["claude-sonnet-5-5"]  # no second identical call


def test_float_free_costs_and_openai_cache_writes() -> None:
    assert cost_usd_micros("claude-haiku-5-5", Usage(30, 0)) == 3  # 30 x $0.10/M, not 4
    assert cost_usd_micros("gpt-6-astra", Usage(0, 0, 0, 8)) == 100  # 8 x $12.50/M


def test_default_clients_have_sdk_retries_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert AnthropicProvider().client.max_retries == 0
    assert OpenAIProvider().client.max_retries == 0


MIXED = [
    Message("user", "move my visit"),
    Message("assistant", "Let me look.", (ToolCall("t1", "get_appointments", {}),)),
    Message("tool", "Thu Oct 22", tool_call_id="t1"),
    Message("assistant", "You have Thu Oct 22."),
    Message("assistant", ""),  # empty turns are dropped, never sent
    Message("user", "earlier please"),
]


def test_anthropic_mixed_turns_cache_and_truncation() -> None:
    seen: dict[str, Any] = {}

    def create(**kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(
            stop_reason="max_tokens",
            content=[],
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with pytest.raises(Truncated):
        AnthropicProvider(client).complete("m", "s", MIXED, [])  # type: ignore[arg-type]
    msgs = seen["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert [b["type"] for b in msgs[1]["content"]] == ["text", "tool_use"]
    assert msgs[2]["content"][0]["type"] == "tool_result" and msgs[4]["content"] == "earlier please"
    assert seen["cache_control"] == {"type": "ephemeral"}


def test_openai_mixed_turns_cache_writes_and_incomplete() -> None:
    seen: dict[str, Any] = {}
    status = ["completed"]

    def create(**kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(
            status=status[0],
            output=[],
            output_text="ok",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=5,
                input_tokens_details=SimpleNamespace(cached_tokens=20, cache_write_tokens=30),
            ),
        )

    p = OpenAIProvider(SimpleNamespace(responses=SimpleNamespace(create=create)))  # type: ignore[arg-type]
    assert p.complete("m", "s", MIXED, [])[2] == Usage(50, 5, 20, 30)
    kinds = [i.get("type", i.get("role")) for i in seen["input"]]
    assert kinds == [
        "user",
        "assistant",
        "function_call",
        "function_call_output",
        "assistant",
        "user",
    ]
    status[0] = "incomplete"
    with pytest.raises(Truncated):
        p.complete("m", "s", MIXED, [])
