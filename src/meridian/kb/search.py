"""Hybrid KB search (D6): keyword (BM25) + meaning (embeddings), merged with reciprocal rank
fusion; returns the top sections. Only current, customer-facing docs are in the index (D5).

The chunk embeddings are precomputed offline (`kb/build_index.py`) and committed with a hash of
every chunk, so startup needs no API call and fails loudly if the KB changed without a rebuild.
Only the query is embedded at search time.
"""

import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from meridian.agent.llm import EMBED_MODEL
from meridian.kb.chunk import Chunk, build_chunks
from meridian.kb.manifest import ROOT

INDEX_DIR = ROOT / "kb/index"
CHUNKS_FILE = INDEX_DIR / "chunks.json"
VECTORS_FILE = INDEX_DIR / "chunk_embeddings.npy"
RRF_K = 60
TOP_K = 4

Embedder = Callable[[list[str]], np.ndarray]
MODES = ("hybrid", "bm25", "embeddings")  # bm25 / embeddings alone are eval baselines

_STOP = frozenset(
    "a an and are as at be but by can do does for from get got how i i'm if in is it its "
    "me my of on or our so that the their them there this to us was we what when where "
    "which who why will with you your".split()
)
_TOKEN = re.compile(r"\$?\d+(?:[.,]\d+)*%?|[a-z]+(?:[-'][a-z]+)*")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class BM25:
    """Okapi BM25 over the chunk texts (k1=1.5, b=0.75)."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs, self.k1, self.b = docs, k1, b
        self.avg = sum(map(len, docs)) / max(len(docs), 1)
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, query: list[str]) -> list[float]:
        out: list[float] = []
        for d, tf in zip(self.docs, self.tf, strict=True):
            s = 0.0
            for t in query:
                if t in tf:
                    f = tf[t]
                    s += (
                        self.idf[t]
                        * f
                        * (self.k1 + 1)
                        / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
                    )
            out.append(s)
        return out


def rrf(rankings: list[list[int]], k: int = RRF_K) -> list[int]:
    """Reciprocal rank fusion: merge several rankings (lists of item indexes, best first)."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            score[item] = score.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda i: (-score[i], i))


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    rank: int


class Index:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray, embed: Embedder) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        self.chunks = chunks
        self.vectors = vectors / safe_norm(vectors)
        self.bm25 = BM25([tokenize(c.text) for c in chunks])
        self.embed = embed

    def search(self, query: str, k: int = TOP_K, mode: str = "hybrid") -> list[Hit]:
        rankings: list[list[int]] = []
        if mode in ("hybrid", "bm25"):
            kw = self.bm25.scores(tokenize(query))
            rankings.append([i for i in sorted(range(len(kw)), key=lambda i: -kw[i]) if kw[i] > 0])
        if mode in ("hybrid", "embeddings"):
            q: np.ndarray = self.embed([query])[0]
            sem: list[float] = (self.vectors @ (q / safe_norm(q))).tolist()
            rankings.append(sorted(range(len(sem)), key=lambda i: -sem[i]))
        if not rankings:
            raise ValueError(f"unknown search mode {mode!r}")
        merged = rrf(rankings)
        return [Hit(self.chunks[i], r + 1) for r, i in enumerate(merged[:k])]


def safe_norm(v: np.ndarray) -> np.ndarray:
    """Vector length, floored so an all-zero vector scores 0 instead of NaN."""
    return np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)


def save_index(
    chunks: list[Chunk], vectors: np.ndarray, out: Path = INDEX_DIR, model: str = EMBED_MODEL
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "model": model,
        "chunks": [{"id": c.id, "doc": c.doc_path, "sha": c.sha} for c in chunks],
    }
    (out / CHUNKS_FILE.name).write_text(json.dumps(meta, indent=1) + "\n")
    np.save(out / VECTORS_FILE.name, vectors.astype(np.float16))


def load_index(embed: Embedder) -> Index:
    """Rebuild chunks from the KB and check them against the committed index."""
    chunks = build_chunks()
    meta = json.loads(CHUNKS_FILE.read_text())
    if meta["model"] != EMBED_MODEL or [(c.id, c.sha) for c in chunks] != [
        (m["id"], m["sha"]) for m in meta["chunks"]
    ]:
        raise RuntimeError("KB index is stale: run `python -m kb.build_index`")
    return Index(chunks, np.load(VECTORS_FILE).astype(np.float32), embed)
