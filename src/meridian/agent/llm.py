"""Model adapter (D7). The only module that imports model SDKs (architecture rule 3).

Embeddings (T5) and the chat models (T9). The chat side uses neutral message/tool types so the
agent graph never sees an SDK type. Every call has a timeout; a failure is retried once (unless
retrying can't help), then the backup provider runs the same request; if that fails too,
ModelUnavailable tells the graph to hand off politely (rule 12).
"""

import json
import logging
import os
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import anthropic
import numpy as np
import openai

log = logging.getLogger(__name__)

EMBED_MODEL = "text-embedding-3-small"
EMBED_TIMEOUT_S = 15.0


def provider_reachable() -> bool:
    """A free call (list models) with the real key, for `/health?deep=1` (D15.6)."""
    try:
        anthropic.Anthropic(max_retries=0, timeout=5).models.list(limit=1)
        return True
    except Exception as exc:  # any failure means "not reachable"; log the type only
        log.warning("model provider check failed: %s", type(exc).__name__)
        return False


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed texts with OpenAI; returns float32 rows (not normalized). Raises on failure."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")
    client = openai.OpenAI(max_retries=2, timeout=EMBED_TIMEOUT_S)
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return np.array([d.embedding for d in resp.data], dtype=np.float32)


# --- Chat models (D7, T9) -------------------------------------------------------------------

CHAT_TIMEOUT_S = 20.0  # per attempt; worst case before the handoff is 3 attempts
MAX_TOKENS = 1024
PRIMARY = {"brain": "claude-sonnet-5-5", "supervisor": "claude-haiku-5-5"}
BACKUP_MODEL = "gpt-6-astra"

# Integer micro-dollars per million tokens (rule 16): (input, output, cache read, cache write).
# Official pricing pages, checked 2026-10-19. Haiku's higher tier starts above 100k prompt
# tokens (its page lists cache prices for both tiers); gpt-6-astra's long-context tier starts
# above 272k (its cache-write price there isn't listed; assumed unchanged). Our prompts are ~5k.
_PRICES: dict[str, tuple[tuple[int, tuple[int, int, int, int]], ...]] = {
    "claude-sonnet-5-5": ((0, (2_000_000, 10_000_000, 100_000, 2_500_000)),),
    "claude-haiku-5-5": (
        (0, (100_000, 500_000, 10_000, 125_000)),
        (100_001, (500_000, 2_500_000, 50_000, 625_000)),
    ),
    "gpt-6-astra": (
        (0, (10_000_000, 50_000_000, 1_000_000, 12_500_000)),
        (272_001, (20_000_000, 75_000_000, 2_000_000, 12_500_000)),
    ),
}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # for role == "tool"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema for the arguments


@dataclass(frozen=True)
class Usage:
    input: int = 0  # uncached prompt tokens
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass(frozen=True)
class Reply:
    text: str
    tool_calls: tuple[ToolCall, ...]
    model: str
    usage: Usage
    cost_usd_micros: int  # our spend, integer micro-dollars (rule 16)
    used_backup: bool = False
    latency_ms: int = 0  # whole chat() call, including failed attempts


class ModelUnavailable(Exception):
    """Primary (after one retry) and backup both failed; the graph hands off politely."""


class Truncated(Exception):
    """The model stopped at the token limit; an incomplete reply counts as a failure."""


class BadReply(Exception):
    """The model's reply was unusable (refusal, malformed tool arguments): a model failure."""


class Provider(Protocol):
    def complete(
        self, model: str, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> tuple[str, tuple[ToolCall, ...], Usage]: ...


def cost_usd_micros(model: str, u: Usage) -> int:
    """Exact integer cost, rounded up to the next micro-dollar."""
    prompt = u.input + u.cache_read + u.cache_write
    p_in, p_out, p_read, p_write = next(
        p for start, p in reversed(_PRICES[model]) if prompt >= start
    )
    total = u.input * p_in + u.output * p_out + u.cache_read * p_read + u.cache_write * p_write
    return -(-total // 1_000_000)


# Errors where a second identical call can't succeed: skip straight to the backup.
_NO_RETRY: tuple[type[Exception], ...] = (
    anthropic.BadRequestError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    openai.BadRequestError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    Truncated,
)
_SDK_ERRORS: tuple[type[Exception], ...] = (
    anthropic.APIError,
    openai.APIError,
    Truncated,
    BadReply,
)


class AnthropicProvider:
    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        # SDK retries off: our policy (one retry, then backup) is the only one
        self.client = client or anthropic.Anthropic(max_retries=0, timeout=CHAT_TIMEOUT_S)

    def complete(
        self, model: str, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> tuple[str, tuple[ToolCall, ...], Usage]:
        msgs: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                pending_results.append(
                    {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text}
                )
                continue
            if pending_results:  # parallel tool results go back in one user turn
                msgs.append({"role": "user", "content": pending_results})
                pending_results = []
            if m.role == "assistant":
                content: list[dict[str, Any]] = [{"type": "text", "text": m.text}] if m.text else []
                content += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                    for c in m.tool_calls
                ]
                if content:  # an empty assistant turn is a 400 at the API
                    msgs.append({"role": "assistant", "content": content})
            else:
                msgs.append({"role": "user", "content": m.text})
        if pending_results:
            msgs.append({"role": "user", "content": pending_results})
        resp = self.client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=msgs,  # type: ignore[arg-type]
            tools=[
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ],  # type: ignore[misc]
            cache_control={"type": "ephemeral"},  # caches the prompt prefix up to the last block
            timeout=CHAT_TIMEOUT_S,
        )
        if resp.stop_reason == "max_tokens":
            raise Truncated
        if resp.stop_reason == "refusal":
            raise BadReply
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = tuple(
            ToolCall(b.id, b.name, dict(b.input))  # type: ignore[arg-type]
            for b in resp.content
            if b.type == "tool_use"
        )
        u = resp.usage
        return (
            text,
            calls,
            Usage(
                u.input_tokens,
                u.output_tokens,
                u.cache_read_input_tokens or 0,
                u.cache_creation_input_tokens or 0,
            ),
        )


class OpenAIProvider:
    def __init__(self, client: openai.OpenAI | None = None) -> None:
        self.client = client or openai.OpenAI(max_retries=0, timeout=CHAT_TIMEOUT_S)

    def complete(
        self, model: str, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> tuple[str, tuple[ToolCall, ...], Usage]:
        items: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                items.append(
                    {"type": "function_call_output", "call_id": m.tool_call_id, "output": m.text}
                )
            elif m.role == "assistant":
                if m.text:
                    items.append({"role": "assistant", "content": m.text})
                items += [
                    {
                        "type": "function_call",
                        "call_id": c.id,
                        "name": c.name,
                        "arguments": json.dumps(c.args),
                    }
                    for c in m.tool_calls
                ]
            else:
                items.append({"role": "user", "content": m.text})
        resp = self.client.responses.create(
            model=model,
            instructions=system,
            input=items,  # type: ignore[arg-type]
            tools=[
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "strict": False,
                }
                for t in tools
            ],  # type: ignore[misc]
            max_output_tokens=MAX_TOKENS,
            store=False,  # OpenAI keeps no copy of the conversation (D17)
            timeout=CHAT_TIMEOUT_S,
        )
        if resp.status == "incomplete":
            raise Truncated
        try:
            calls = tuple(
                ToolCall(o.call_id, o.name, json.loads(o.arguments or "{}"))
                for o in resp.output
                if o.type == "function_call"
            )
        except json.JSONDecodeError as exc:
            raise BadReply from exc
        u = resp.usage
        if u is None:
            return resp.output_text, calls, Usage()
        d = u.input_tokens_details
        read, write = (d.cached_tokens or 0), (getattr(d, "cache_write_tokens", 0) or 0)
        return (
            resp.output_text,
            calls,
            Usage(u.input_tokens - read - write, u.output_tokens, read, write),
        )


@dataclass
class LLM:
    """Model adapter used by the agent graph: primary with one retry, then backup (D7)."""

    primary: Provider = field(default_factory=AnthropicProvider)
    backup: Provider = field(default_factory=OpenAIProvider)
    models: dict[str, str] = field(default_factory=lambda: dict(PRIMARY))
    backup_model: str = BACKUP_MODEL

    def chat(
        self,
        role: Literal["brain", "supervisor"],
        system: str,
        messages: Sequence[Message],
        tools: Sequence[Tool] = (),
    ) -> Reply:
        start = time.monotonic()
        plan = [(self.primary, self.models[role], False)] * 2 + [
            (self.backup, self.backup_model, True)
        ]
        i = 0
        while i < len(plan):
            provider, model, is_backup = plan[i]
            try:
                text, calls, usage = provider.complete(model, system, messages, tools)
            except Exception as exc:  # any failure fails over; logged without content (D17)
                status = getattr(exc, "status_code", None)
                if isinstance(exc, _SDK_ERRORS):
                    log.warning("model call failed: %s %s %s", model, type(exc).__name__, status)
                else:  # likely our own bug (conversion/parsing): loud, but still fail over
                    # traceback lines only: an exception message can quote caller text (D17)
                    frames = "".join(traceback.format_tb(exc.__traceback__))
                    log.error("model call raised %s on %s\n%s", type(exc).__name__, model, frames)
                i = 2 if (not is_backup and isinstance(exc, _NO_RETRY)) else i + 1
                continue
            return Reply(
                text,
                calls,
                model,
                usage,
                cost_usd_micros(model, usage),
                is_backup,
                int((time.monotonic() - start) * 1000),
            )
        raise ModelUnavailable
