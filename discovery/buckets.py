"""Discovery step 2 (D2): propose buckets from the 420 pass-1 lines, then cross-check
with embeddings + k-means. Output: discovery/buckets_draft.md (for review and sign-off).

Run: uv run --env-file .env python -m discovery.buckets
The Opus reply (JSON) and the embeddings (float16 .npy) are committed under
discovery/cache/, so a fresh clone re-renders the same draft with no API calls.
"""

import json
import sys
from pathlib import Path
from typing import Any

import anthropic
import numpy as np
import openai

from discovery.common import CACHE, load_cache, parse_json_object
from discovery.pass1 import PROMPT_VERSION as PASS1_VERSION

MODEL = "claude-opus-5-5"
EMBED_MODEL = "text-embedding-3-small"
PASS1 = CACHE / "pass1.jsonl"
BUCKETS_CACHE = CACHE / "buckets_opus.json"
CALL_EMBED = CACHE / "pass1_embeddings.npy"
BUCKET_EMBED = CACHE / "bucket_embeddings.npy"
OUT = Path("discovery/buckets_draft.md")
SEED = 7
MAX_BUCKETS = 10
WEAK_MATCH = 0.45  # cluster centroid vs nearest bucket definition; below this, flag it

SYSTEM = """You are helping an engineer find why customers call a residential HVAC and plumbing
company. You get one line per call (why they called, an optional second reason, the outcome).
Propose a bucket list: mutually exclusive categories for the PRIMARY reason, specific enough
to decide what an AI agent could fully handle, with at most 10 buckets.
Base it only on the lines; do not invent categories with no calls.

Reply with only a JSON object:
{"buckets": [{"id": "<snake_case>", "name": "<short name>",
  "definition": "<one sentence>", "includes": "<what counts>", "excludes": "<what doesn't>",
  "examples": ["<call_id>", "<call_id>", "<call_id>"], "estimated_count": <int>}],
 "notes": "<anything ambiguous the engineer should decide>"}"""

_TEXT_FIELDS = ("id", "name", "definition", "includes", "excludes")


def lines(rows: dict[str, dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for cid in sorted(rows):
        r = rows[cid]
        second = f" | also: {r['second_reason']}" if r["second_reason"] else ""
        out.append(f"{cid}: {r['reason']}{second} | outcome: {r['outcome']}")
    return out


def validate_proposal(obj: dict[str, Any], known_ids: set[str]) -> dict[str, Any]:
    """Shape check of untrusted model JSON; drops invented example IDs.

    Raises ValueError on anything unusable. Values are converted, not narrowed with
    isinstance, so strict typing stays on `Any` instead of partially-unknown types.
    """
    try:
        buckets: list[Any] = list(obj["buckets"])
        if not 2 <= len(buckets) <= MAX_BUCKETS:
            raise ValueError(f"need 2-{MAX_BUCKETS} buckets")
        seen: set[str] = set()
        clean: list[dict[str, Any]] = []
        for raw in buckets:
            b: dict[str, Any] = dict(raw)
            fields = {f: str(b[f]).strip() for f in _TEXT_FIELDS}
            if not all(fields.values()):
                raise ValueError("bucket has an empty text field")
            if fields["id"] in seen:
                raise ValueError(f"duplicate bucket id {fields['id']}")
            seen.add(fields["id"])
            examples = [str(e) for e in list(b.get("examples", [])) if str(e) in known_ids]
            if not examples:
                raise ValueError(f"bucket {fields['id']} has no real example calls")
            count = b.get("estimated_count")
            est = count if type(count) is int else 0
            clean.append({**fields, "examples": examples, "estimated_count": est})
    except (KeyError, TypeError) as exc:
        raise ValueError(f"bad bucket proposal: {exc}") from exc
    return {"buckets": clean, "notes": str(obj.get("notes", ""))}


def propose(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if BUCKETS_CACHE.exists():
        return validate_proposal(json.loads(BUCKETS_CACHE.read_text()), set(rows))
    client = anthropic.Anthropic(max_retries=3)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=16000,  # Opus thinks before answering; leave room for the JSON
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines(rows))}],
        timeout=170.0,
    )
    raw = parse_json_object("".join(b.text for b in msg.content if b.type == "text"))
    proposal = validate_proposal(raw, set(rows))
    BUCKETS_CACHE.write_text(json.dumps(proposal, indent=2))
    return proposal


def embed(texts: list[str], cache: Path) -> np.ndarray:
    if cache.exists():
        return np.load(cache).astype(np.float32)
    client = openai.OpenAI(max_retries=3, timeout=60.0)
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    arr = np.array([d.embedding for d in resp.data], dtype=np.float32)
    np.save(cache, arr.astype(np.float16))  # ~1.3 MB for 420 calls; precision is ample
    return arr


def normalize(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def kmeans(x: np.ndarray, k: int, iters: int = 50) -> np.ndarray:
    """Cosine k-means (rows normalized), deterministic seed. Returns a label per row."""
    rng = np.random.default_rng(SEED)
    x = normalize(x)
    centers = x[rng.choice(len(x), size=k, replace=False)]
    labels = np.zeros(len(x), dtype=int)
    for _ in range(iters):
        labels = np.argmax(x @ centers.T, axis=1)
        new = normalize(
            np.stack(
                [
                    x[labels == j].mean(axis=0) if np.any(labels == j) else centers[j]
                    for j in range(k)
                ]
            )
        )
        if np.allclose(new, centers):
            break
        centers = new
    return labels


def cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(
    rows: dict[str, dict[str, Any]],
    proposal: dict[str, Any],
    clusters: list[tuple[list[str], str, float]],
) -> str:
    buckets = proposal["buckets"]
    total = sum(b["estimated_count"] for b in buckets)
    out = [
        "# Discovery: bucket list (DRAFT — for review and owner sign-off)",
        "",
        f"_Proposed by `{MODEL}` from {len(rows)} pass-1 lines (`discovery/cache/pass1.jsonl`, "
        "Haiku, no category list given). Cross-check: `text-embedding-3-small` + cosine k-means; "
        "each cluster is matched to the bucket whose definition is closest. Counts are the "
        f"proposer's estimates (they sum to {total}, not {len(rows)}); real counts come from the "
        "two blind labelers in T3._",
        "",
        "## Proposed buckets",
        "",
        "| id | name | est. calls | definition | includes | excludes | examples |",
        "|---|---|---|---|---|---|---|",
    ]
    for b in buckets:
        out.append(
            f"| `{cell(b['id'])}` | {cell(b['name'])} | {b['estimated_count']} | "
            f"{cell(b['definition'])} | {cell(b['includes'])} | {cell(b['excludes'])} | "
            f"{', '.join(b['examples'])} |"
        )
    out += ["", "**Proposer's notes:** " + cell(proposal["notes"]), ""]
    out += [
        "## Cross-check: embedding clusters",
        "",
        f"A cluster whose closest bucket scores below {WEAK_MATCH} is flagged ⚠️ "
        "(possibly something the bucket list doesn't explain).",
        "",
    ]
    for j, (members, bucket_id, sim) in enumerate(clusters):
        flag = " ⚠️ weak match" if sim < WEAK_MATCH else ""
        out.append(f"**Cluster {j + 1}** ({len(members)} calls) → `{bucket_id}` ({sim:.2f}){flag}")
        out += [f"- {cid}: {cell(rows[cid]['reason'])}" for cid in members[:5]]
        out.append("")
    return "\n".join(out)


def main() -> int:
    rows = load_cache(PASS1, PASS1_VERSION)
    if len(rows) != 420:
        print(f"pass 1 incomplete: {len(rows)}/420")
        return 1
    proposal = propose(rows)
    ids = sorted(rows)
    call_vecs = normalize(embed([rows[i]["reason"] for i in ids], CALL_EMBED))
    bucket_texts = [f"{b['name']}: {b['definition']} {b['includes']}" for b in proposal["buckets"]]
    bucket_vecs = normalize(embed(bucket_texts, BUCKET_EMBED))
    k = len(proposal["buckets"]) + 2  # a few extra clusters to surface what buckets miss
    labels = kmeans(call_vecs, k)
    clusters: list[tuple[list[str], str, float]] = []
    for j in range(k):
        idx = np.where(labels == j)[0]
        if len(idx) == 0:
            continue
        centroid = normalize(call_vecs[idx].mean(axis=0, keepdims=True))[0]
        sims = bucket_vecs @ centroid
        best = int(np.argmax(sims))
        clusters.append(([ids[n] for n in idx], proposal["buckets"][best]["id"], float(sims[best])))
    clusters.sort(key=lambda c: len(c[0]), reverse=True)
    OUT.write_text(render(rows, proposal, clusters))
    weak = sum(1 for c in clusters if c[2] < WEAK_MATCH)
    print(f"{len(proposal['buckets'])} buckets; {len(clusters)} clusters; {weak} weak; wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
