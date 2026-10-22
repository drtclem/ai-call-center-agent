"""Scripted fake model and a fake-embedding KB index for agent tests (no network)."""

import hashlib
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from meridian.agent.llm import Message, Reply, Tool, ToolCall, Usage
from meridian.kb.chunk import build_chunks
from meridian.kb.search import Index, tokenize

Step = Reply | Exception | Callable[[str, str, Sequence[Message], Sequence[Tool]], Reply]


def r(text: str = "", *calls: tuple[str, dict[str, Any]]) -> Reply:
    tcs = tuple(ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(calls))
    return Reply(text, tcs, "fake", Usage(), 0)


class FakeLLM:
    """Replays scripted replies in order; fails the test if called more often than scripted."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, Sequence[Message]]] = []

    def chat(
        self, role: str, system: str, messages: Sequence[Message], tools: Sequence[Tool] = ()
    ) -> Reply:
        self.calls.append((role, list(messages)))
        assert self.steps, f"unexpected model call ({role})"
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(role, system, messages, tools) if callable(step) else step

    def add(self, *steps: Step) -> None:
        self.steps += steps


def _embed(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), 64), dtype=np.float32)
    for row, t in enumerate(texts):
        for tok in tokenize(t):
            out[row, int(hashlib.md5(tok.encode()).hexdigest(), 16) % 64] += 1.0
        out[row, 0] += 0.01
    return out


_CHUNKS = build_chunks()
INDEX = Index(_CHUNKS, _embed([c.text for c in _CHUNKS]), _embed)


def route(worker: str, reason: str = "") -> Reply:
    args = {"worker": worker} | ({"reason": reason} if reason else {})
    return r("", ("route", args))
