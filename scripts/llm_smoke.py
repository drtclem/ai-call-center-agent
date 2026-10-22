"""Live smoke test of the model adapter (T9): one tool-using exchange per provider and model.

Opt-in, costs well under a cent: uv run --env-file .env python scripts/llm_smoke.py
"""

import sys

from meridian.agent.llm import (
    BACKUP_MODEL,
    PRIMARY,
    AnthropicProvider,
    Message,
    OpenAIProvider,
    Tool,
    cost_usd_micros,
)

TOOL = Tool(
    "get_hours",
    "Look up Meridian's phone hours for a given day.",
    {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]},
)
SYSTEM = (
    "You are a test assistant. Always call get_hours before answering. "
    "Answer in one short sentence."
)


def run(provider: AnthropicProvider | OpenAIProvider, model: str) -> None:
    msgs = [Message("user", "When are you open on Saturday?")]
    text, calls, usage = provider.complete(model, SYSTEM, msgs, [TOOL])
    assert calls and calls[0].name == "get_hours", (model, text)
    msgs += [
        Message("assistant", text, calls),
        Message("tool", "Saturday 8:00am-4:00pm", tool_call_id=calls[0].id),
    ]
    text2, _, usage2 = provider.complete(model, SYSTEM, msgs, [TOOL])
    assert "8" in text2, (model, text2)
    cost = cost_usd_micros(model, usage) + cost_usd_micros(model, usage2)
    print(
        f"{model}: ok, tool call {calls[0].args}, reply {len(text2)} chars, cost {cost} micro-USD"
    )


def main() -> int:
    a, o = AnthropicProvider(), OpenAIProvider()
    for model in PRIMARY.values():
        run(a, model)
    run(o, BACKUP_MODEL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
