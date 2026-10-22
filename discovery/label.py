"""Discovery pass 2 (D2): two blind labelers assign every call a bucket.

Labeler 1: Claude Opus (reads the transcript, primary + optional secondary).
Labeler 2: Jev via OpenRouter (fixed-choice; secondary = runner-up if >= SECONDARY_MIN).
Neither sees the other's labels. Where primaries differ, OpenAI's flagship suggests a pick
with a one-line reason; the owner decides in the review (D2).

Run: uv run --env-file .env python -m discovery.label {opus|jev|suggest} [--limit N]
Each step caches to discovery/cache/*.jsonl and is resumable.

Cache note: the Opus rows were produced as `label-v1` and re-stamped `label-v2+<hash>` without
re-running (Opus's prompt text and buckets.json were identical); Jev was re-run for v2.
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import anthropic
import httpx
import openai

from discovery.common import (
    CACHE,
    append_cache,
    call_ids,
    load_cache,
    parse_json_object,
    transcript,
    with_retries,
)
from discovery.jev import classify

BUCKETS_FILE = Path("discovery/buckets.json")
OPUS_MODEL = "claude-opus-5-5"
SUGGEST_MODEL = "gpt-6-astra"
PROMPT_VERSION = "label-v2"  # v2: Jev criteria include Excludes, like Opus
SECONDARY_MIN = 0.25
OPUS_CACHE = CACHE / "label_opus.jsonl"
JEV_CACHE = CACHE / "label_jev.jsonl"
SUGGEST_CACHE = CACHE / "label_suggest.jsonl"


def buckets_hash() -> str:
    """Short hash of buckets.json; part of the cache key so edited buckets force a re-label."""
    return hashlib.sha256(BUCKETS_FILE.read_bytes()).hexdigest()[:8]


def cache_version() -> str:
    return f"{PROMPT_VERSION}+{buckets_hash()}"


def load_buckets() -> list[dict[str, Any]]:
    return list(json.loads(BUCKETS_FILE.read_text())["buckets"])


def bucket_ids() -> set[str]:
    return {str(b["id"]) for b in load_buckets()}


def describe(b: dict[str, Any]) -> str:
    """The same bucket description goes to both labelers."""
    return f"{b['name']}. {b['definition']} Includes: {b['includes']} Excludes: {b['excludes']}"


def bucket_text() -> str:
    return "\n".join(f"- {b['id']}: {describe(b)}" for b in load_buckets())


def check_label(obj: dict[str, Any], ids: set[str]) -> dict[str, Any]:
    primary = str(obj.get("primary", ""))
    secondary_raw = obj.get("secondary")
    secondary = None if secondary_raw in (None, "", "null") else str(secondary_raw)
    if primary not in ids:
        raise ValueError(f"unknown primary {primary!r}")
    if secondary is not None and (secondary not in ids or secondary == primary):
        secondary = None  # a bad secondary is dropped, not fatal
    return {"primary": primary, "secondary": secondary, "why": str(obj.get("why", ""))[:300]}


OPUS_SYSTEM = """You label inbound calls to a residential HVAC and plumbing company.
Transcripts come from speech-to-text; ignore filler. Choose the PRIMARY reason for the call from
the bucket list, and a SECONDARY bucket only if the caller clearly raised a second, different
need (otherwise null). Use "other" only if no bucket fits.

Buckets:
{buckets}

Reply with only a JSON object: {{"primary": "<bucket id>", "secondary": "<bucket id or null>",
"why": "<one short sentence>"}}"""


def label_opus(client: anthropic.Anthropic, call_id: str, ids: set[str]) -> dict[str, Any]:
    system = OPUS_SYSTEM.format(buckets=bucket_text())

    def once() -> dict[str, Any]:
        msg = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=8000,
            output_config={"effort": "low"},
            system=system,
            messages=[{"role": "user", "content": transcript(call_id)}],
            timeout=120.0,
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return check_label(parse_json_object(text), ids)

    return {
        "call_id": call_id,
        "model": OPUS_MODEL,
        "prompt_version": cache_version(),
        **with_retries(once),
    }


JEV_INSTRUCTIONS = (
    "Why did this customer call a residential HVAC and plumbing company? "
    "Pick the primary reason. Use 'other' only if nothing fits."
)


def label_jev(client: httpx.Client, call_id: str, ids: set[str]) -> dict[str, Any]:
    criteria: dict[str, str | None] = {str(b["id"]): describe(b) for b in load_buckets()}
    c = classify(client, transcript(call_id), JEV_INSTRUCTIONS, criteria)
    ranked = sorted(c.probabilities.items(), key=lambda kv: -kv[1])
    runner_up = ranked[1] if len(ranked) > 1 else None
    secondary = runner_up[0] if runner_up and runner_up[1] >= SECONDARY_MIN else None
    return {
        "call_id": call_id,
        "model": c.model,
        "prompt_version": cache_version(),
        **check_label({"primary": c.label, "secondary": secondary}, ids),
        "confidence": c.confidence,
        "probabilities": c.probabilities,
    }


SUGGEST_SYSTEM = """Two independent labelers disagreed on why a customer called a residential
HVAC and plumbing company. Read the transcript and pick the better PRIMARY bucket. You may pick
either label or a different bucket. Buckets:
{buckets}

Reply with only a JSON object: {{"pick": "<bucket id>", "why": "<one short sentence>"}}"""


def suggest(client: openai.OpenAI, call_id: str, a: str, b: str, ids: set[str]) -> dict[str, Any]:
    def once() -> dict[str, Any]:
        resp = client.chat.completions.create(
            model=SUGGEST_MODEL,
            messages=[
                {"role": "system", "content": SUGGEST_SYSTEM.format(buckets=bucket_text())},
                {
                    "role": "user",
                    "content": f"Label A: {a}\nLabel B: {b}\n\nTranscript:\n{transcript(call_id)}",
                },
            ],
        )
        obj = parse_json_object(resp.choices[0].message.content or "")
        pick = str(obj.get("pick", ""))
        if pick not in ids:
            raise ValueError(f"unknown pick {pick!r}")
        return {"pick": pick, "why": str(obj.get("why", ""))[:300]}

    return {
        "call_id": call_id,
        "model": SUGGEST_MODEL,
        "prompt_version": cache_version(),
        **with_retries(once),
    }


def run_pool(
    todo: list[str], work: Callable[[str], dict[str, Any]], cache: Path, workers: int
) -> int:
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, c): c for c in todo}
        for fut in as_completed(futures):
            try:
                append_cache(cache, fut.result())  # main thread only: no write race
            except (
                anthropic.APIError,
                openai.APIError,
                httpx.HTTPError,
                RuntimeError,
                ValueError,
                KeyError,
            ) as exc:
                failed += 1
                print(f"{futures[fut]}: failed ({type(exc).__name__}: {str(exc)[:120]})")
    return failed


def disagreements() -> list[tuple[str, str, str]]:
    opus = load_cache(OPUS_CACHE, cache_version())
    jev = load_cache(JEV_CACHE, cache_version())
    return [
        (cid, opus[cid]["primary"], jev[cid]["primary"])
        for cid in call_ids()
        if cid in opus and cid in jev and opus[cid]["primary"] != jev[cid]["primary"]
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["opus", "jev", "suggest"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    ids = bucket_ids()

    if args.step == "opus":
        done = load_cache(OPUS_CACHE, cache_version())
        todo = [c for c in call_ids() if c not in done][: args.limit]
        client = anthropic.Anthropic(max_retries=3)
        failed = run_pool(todo, lambda c: label_opus(client, c, ids), OPUS_CACHE, args.workers)
        total = len(load_cache(OPUS_CACHE, cache_version()))
    elif args.step == "jev":
        done = load_cache(JEV_CACHE, cache_version())
        todo = [c for c in call_ids() if c not in done][: args.limit]
        with httpx.Client() as http:
            failed = run_pool(todo, lambda c: label_jev(http, c, ids), JEV_CACHE, args.workers)
        total = len(load_cache(JEV_CACHE, cache_version()))
    else:
        done = load_cache(SUGGEST_CACHE, cache_version())
        pending = [(c, a, b) for c, a, b in disagreements() if c not in done][: args.limit]
        todo = [c for c, _, _ in pending]
        pairs = {c: (a, b) for c, a, b in pending}
        client = openai.OpenAI(max_retries=3, timeout=120.0)
        failed = run_pool(
            todo, lambda c: suggest(client, c, *pairs[c], ids), SUGGEST_CACHE, args.workers
        )
        total = len(load_cache(SUGGEST_CACHE, cache_version()))
    print(f"{args.step}: did {len(todo)}, failed {failed}, cached {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
