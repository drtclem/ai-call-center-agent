"""Retrieval eval (D6, T5): does the right section come back before we trust any answer?

Run: uv run --env-file .env python -m evals.retrieval.run   (live: embeds each question)
Writes evals/retrieval/results/latest.md and latest.json.
Headline = section hit: a top-3 section from an expected doc contains one of the question's
verbatim `answer` phrases. Document hits (any expected doc in the top 3) are shown alongside.
Keyword-only and embeddings-only rows are baselines for the hybrid search the agent uses.
Separately, every result is checked: no superseded, stub or agent-only doc may ever be returned.
"""

import json
import sys
import tomllib
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from meridian.agent.llm import embed_texts
from meridian.kb.chunk import Chunk
from meridian.kb.manifest import load
from meridian.kb.search import MODES, Index, load_index

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.toml"
RESULTS = HERE / "results"
TOP = 3


def load_questions(path: Path = QUESTIONS) -> list[dict[str, Any]]:
    return list(tomllib.loads(path.read_text())["q"])


def answers(chunk: Chunk, q: dict[str, Any]) -> bool:
    """The section is from an expected doc and holds one of the verbatim answer phrases."""
    text = chunk.text.lower()
    return chunk.doc_path in q["expected"] and any(a.lower() in text for a in q.get("answer", []))


def _score(index: Index, q: dict[str, Any], mode: str) -> dict[str, Any]:
    hits = index.search(q["question"], k=4, mode=mode)
    sec = next((i + 1 for i, h in enumerate(hits) if answers(h.chunk, q)), None)
    return {
        "returned": [h.chunk.id for h in hits],
        "section_rank": sec,  # 1-4, or None
        "doc_hit": any(h.chunk.doc_path in q["expected"] for h in hits[:TOP]),
    }


def _summary(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    r = [x[mode] for x in rows]
    return {
        "section_top3": sum(1 for x in r if x["section_rank"] and x["section_rank"] <= TOP),
        "section_top4": sum(1 for x in r if x["section_rank"]),
        "doc_top3": sum(x["doc_hit"] for x in r),
        "mrr_top3": round(
            sum(1 / x["section_rank"] for x in r if x["section_rank"] and x["section_rank"] <= TOP)
            / max(len(r), 1),
            3,
        ),
    }


def evaluate(index: Index, questions: list[dict[str, Any]]) -> dict[str, Any]:
    allowed = {d.path for d in load() if d.searchable}
    by_id = {c.id: c for c in index.chunks}
    rows: list[dict[str, Any]] = []
    for q in questions:
        row: dict[str, Any] = {
            "id": q["id"],
            "bucket": q["bucket"],
            "source": q["source"],
            "question": q["question"],
            "expected": sorted(q["expected"]),
            "answer": q.get("answer", []),
            "scored": bool(q["expected"]),
            "note": q.get("note", ""),
        }
        for mode in MODES:
            row[mode] = _score(index, q, mode)
        row["forbidden"] = sorted(
            {by_id[c].doc_path for m in MODES for c in row[m]["returned"]} - allowed
        )
        rows.append(row)
    scored = [r for r in rows if r["scored"]]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in scored:
        groups[f"bucket: {r['bucket']}"].append(r)
        groups[f"source: {r['source']}"].append(r)
    return {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),  # run stamp, not a business date
        "questions": len(rows),
        "scored": len(scored),
        "modes": {m: _summary(scored, m) for m in MODES},
        "groups": {
            g: [_summary(rs, "hybrid")["section_top3"], len(rs)] for g, rs in sorted(groups.items())
        },
        "forbidden_returned": sum(len(r["forbidden"]) for r in rows),
        "rows": rows,
    }


def render(res: dict[str, Any]) -> str:
    n = res["scored"]
    h = res["modes"]["hybrid"]

    def pct(x: int) -> str:
        return f"{x}/{n} ({x / n:.0%})"

    out = [
        "# Retrieval eval",
        "",
        f"_Run {res['run_at']}; {res['questions']} questions (`evals/retrieval/questions.toml`), "
        f"{n} scored. Search: BM25 + `text-embedding-3-small`, merged with reciprocal rank "
        "fusion over section chunks of current customer docs (D6)._",
        "",
        f"- **Answering section in the top 3: {pct(h['section_top3'])}**; mean reciprocal rank "
        f"{h['mrr_top3']}.",
        f"- Answering section anywhere in the 4 sections the agent receives: "
        f"{pct(h['section_top4'])}.",
        f"- Right document in the top 3 (looser): {pct(h['doc_top3'])}.",
        f"- Old, stub or internal documents returned (any mode): **{res['forbidden_returned']}**.",
        "",
        "| Search | Section top 3 | Section top 4 | Doc top 3 | MRR |",
        "|---|---|---|---|---|",
        *[
            f"| {m}{' (used by the agent)' if m == 'hybrid' else ' (baseline)'} | "
            f"{s['section_top3']} | {s['section_top4']} | {s['doc_top3']} | {s['mrr_top3']} |"
            for m, s in res["modes"].items()
        ],
        "",
        "| Group | Section hits, top 3 (hybrid) |",
        "|---|---|",
        *[f"| {g} | {x}/{t} |" for g, (x, t) in res["groups"].items()],
        "",
        "## Misses (hybrid, answering section not in the top 3)",
        "",
    ]
    misses = [
        r
        for r in res["rows"]
        if r["scored"] and not (r["hybrid"]["section_rank"] and r["hybrid"]["section_rank"] <= TOP)
    ]
    out += [
        f"- **{r['id']}** ({r['source']}) {r['question']} — answering section rank: "
        f"{r['hybrid']['section_rank'] or 'not in top 4'}; top 3: "
        f"{', '.join(r['hybrid']['returned'][:TOP])}" + (f". _{r['note']}_" if r["note"] else "")
        for r in misses
    ] or ["- none"]
    out += ["", "## Unscored probes", ""]
    out += [
        f"- **{r['id']}** {r['question']} — returned {', '.join(r['hybrid']['returned'][:TOP])}"
        for r in res["rows"]
        if not r["scored"]
    ] or ["- none"]
    return "\n".join(out) + "\n"


def main() -> int:
    res = evaluate(load_index(embed_texts), load_questions())
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest.json").write_text(json.dumps(res, indent=1) + "\n")
    (RESULTS / "latest.md").write_text(render(res))
    h = res["modes"]["hybrid"]
    print(
        f"section top-3 {h['section_top3']}/{res['scored']}; forbidden {res['forbidden_returned']}"
    )
    return 1 if res["forbidden_returned"] else 0


if __name__ == "__main__":
    sys.exit(main())
