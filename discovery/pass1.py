"""Discovery pass 1 (D2): one open-ended line per call — no bucket list given.

Run: uv run --env-file .env python -m discovery.pass1 [--limit N] [--workers 8]
Results are cached in discovery/cache/pass1.jsonl; re-runs only call the API for
missing calls, so the committed cache reproduces everything without API calls.
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import anthropic

from discovery.common import (
    CACHE,
    append_cache,
    call_ids,
    load_cache,
    parse_json_object,
    transcript,
    with_retries,
)

MODEL = "claude-haiku-5-5"
PROMPT_VERSION = "p1-v1"
CACHE_FILE = CACHE / "pass1.jsonl"

SYSTEM = """You read one inbound call transcript from a residential HVAC and plumbing company.
Transcripts come from speech-to-text and contain filler and interruptions; ignore those.
Describe the call in your own words. Do NOT use a predefined category list.

Reply with only a JSON object:
{"reason": "<one line: why the customer called, specific>",
 "second_reason": "<one line, or null if there was only one reason>",
 "outcome": "<resolved_on_call | escalated | callback_promised | unresolved>",
 "account_lookup": <true if the agent looked up or verified an account>}"""

OUTCOMES = {"resolved_on_call", "escalated", "callback_promised", "unresolved"}


def validate(obj: dict[str, Any]) -> dict[str, Any]:
    reason = obj["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("empty reason")
    second = obj.get("second_reason")
    if second is not None and not isinstance(second, str):
        raise ValueError("bad second_reason")
    if not isinstance(obj["outcome"], str) or obj["outcome"] not in OUTCOMES:
        raise ValueError("bad outcome")
    if not isinstance(obj["account_lookup"], bool):
        raise ValueError("bad account_lookup")
    return {
        "reason": reason.strip(),
        "second_reason": (second or "").strip() or None,
        "outcome": obj["outcome"],
        "account_lookup": obj["account_lookup"],
    }


def summarize(client: anthropic.Anthropic, call_id: str) -> dict[str, Any]:
    def once() -> dict[str, Any]:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM,
            messages=[{"role": "user", "content": transcript(call_id)}],
            timeout=60.0,
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return validate(parse_json_object(text))

    out = with_retries(once)
    return {"call_id": call_id, "model": MODEL, "prompt_version": PROMPT_VERSION, **out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    done = load_cache(CACHE_FILE, PROMPT_VERSION)
    todo = [c for c in call_ids() if c not in done][: args.limit]
    print(f"cached {len(done)}, to do {len(todo)}")
    if not todo:
        return 0
    client = anthropic.Anthropic(max_retries=3)
    failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(summarize, client, c): c for c in todo}
        for fut in as_completed(futures):
            try:
                append_cache(CACHE_FILE, fut.result())
            except (anthropic.APIError, ValueError, KeyError) as exc:
                failed += 1
                print(f"{futures[fut]}: failed ({type(exc).__name__})")
    print(f"done; failed {failed}; cached {len(load_cache(CACHE_FILE, PROMPT_VERSION))}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
