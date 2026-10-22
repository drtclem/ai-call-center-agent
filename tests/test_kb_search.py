import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from evals.retrieval.run import answers, evaluate, load_questions
from meridian.kb.chunk import Chunk, build_chunks, pieces
from meridian.kb.manifest import load
from meridian.kb.search import BM25, CHUNKS_FILE, Index, load_index, rrf, safe_norm, tokenize


def fake_embed(texts: list[str]) -> np.ndarray:
    """Deterministic bag-of-words hashing: stands in for the embedding API in tests."""
    out = np.zeros((len(texts), 64), dtype=np.float32)
    for row, t in enumerate(texts):
        for tok in tokenize(t):
            out[row, int(hashlib.md5(tok.encode()).hexdigest(), 16) % 64] += 1.0
        out[row, 0] += 0.01  # never all-zero
    return out


def test_tokenize_keeps_prices_and_drops_stopwords() -> None:
    assert tokenize("Is the $149 fee waived for members?") == ["$149", "fee", "waived", "members"]


def test_bm25_ranks_the_matching_doc_first() -> None:
    bm = BM25([tokenize("membership price per year"), tokenize("warranty labor coverage")])
    s = bm.scores(tokenize("warranty coverage"))
    assert s[1] > s[0] == 0.0


def test_rrf_rewards_items_ranked_well_by_both() -> None:
    assert rrf([[0, 1, 2], [1, 0, 2]])[:2] in ([0, 1], [1, 0])
    assert rrf([[2, 1], [2, 0]])[0] == 2


def test_chunks_only_from_searchable_docs_and_tables_repeat_headers() -> None:
    allowed = {d.path for d in load() if d.searchable}
    chunks = build_chunks()
    assert {c.doc_path for c in chunks} <= allowed
    fee = [c for c in chunks if c.doc_path == "pricing/2026_fee_schedule.pdf"]
    assert len(fee) >= 2
    for c in fee:
        assert "| Fee | Non-member | Comfort Club | Comfort Club Plus |" in c.text
        assert c.text.startswith("[Meridian Home Comfort — 2026 Residential Fee Schedule")


def test_provenance_notes_are_not_indexed_and_short_lists_stay_whole() -> None:
    chunks = build_chunks()
    assert not any("Transcribed from" in c.text or "Converted from" in c.text for c in chunks)
    assert not any("waive" in c.text.lower() and "technician_arrival" in c.doc_path for c in chunks)
    holiday = [c for c in chunks if c.doc_path == "policies/holiday_hours_2026.png"]
    assert len(holiday) == 1 and "$199" in holiday[0].text and "Thanksgiving" in holiday[0].text


def test_committed_index_matches_the_kb() -> None:
    index = load_index(fake_embed)
    assert len(index.chunks) == len(json.loads(CHUNKS_FILE.read_text())["chunks"])


def test_stale_index_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    real = build_chunks()
    changed = [Chunk(c.id, c.doc_path, c.heading, c.effective, c.text + " edited") for c in real]
    monkeypatch.setattr("meridian.kb.search.build_chunks", lambda: changed)
    with pytest.raises(RuntimeError, match="stale"):
        load_index(fake_embed)


def test_index_from_another_embedding_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meridian.kb.search.EMBED_MODEL", "some-other-model")
    with pytest.raises(RuntimeError, match="stale"):
        load_index(fake_embed)


def test_search_finds_keyword_specific_sections_with_any_embedder() -> None:
    chunks = build_chunks()
    index = Index(chunks, fake_embed([c.text for c in chunks]), fake_embed)
    top = [h.chunk.doc_path for h in index.search("Thanksgiving holiday hours", k=3)]
    assert "policies/holiday_hours_2026.png" in top
    assert len(index.search("anything", k=4)) == 4


def test_questions_file_is_valid() -> None:
    qs = load_questions()
    allowed = {d.path for d in load() if d.searchable}
    assert len({q["id"] for q in qs}) == len(qs) >= 60
    for q in qs:
        assert set(q["expected"]) <= allowed, q["id"]


def test_every_answer_phrase_is_in_an_expected_doc() -> None:
    """Catches typos and answer phrases that a KB or chunking change silently removed."""
    chunks = build_chunks()
    for q in load_questions():
        assert q["source"] in ("transcript", "kb", "paraphrase"), q["id"]
        if q["expected"]:
            assert q.get("answer"), q["id"]
            for phrase in q["answer"]:
                assert any(answers(c, {**q, "answer": [phrase]}) for c in chunks), (q["id"], phrase)


def test_section_hit_needs_the_answer_not_just_the_doc() -> None:
    c = Chunk("d#a-0", "d", "a", None, "[D]\n- Late cancellation fee: $49")
    assert answers(c, {"expected": ["d"], "answer": ["$49"]})
    assert not answers(c, {"expected": ["d"], "answer": ["waived"]})
    assert not answers(c, {"expected": ["other"], "answer": ["$49"]})


def test_evaluate_scores_a_known_index_and_flags_forbidden_docs() -> None:
    a = Chunk(
        "faq/customer_faq.md#fees-0", "faq/customer_faq.md", "Fees", None, "diagnostic fee $89"
    )
    b = Chunk("faq/faq_old_copy.md#x-0", "faq/faq_old_copy.md", "Old", None, "diagnostic fee $79")
    vecs = fake_embed([a.text, b.text])
    q = {
        "id": "X",
        "bucket": "b",
        "source": "kb",
        "question": "diagnostic fee",
        "expected": ["faq/customer_faq.md"],
        "answer": ["$89"],
    }
    res = evaluate(Index([a, b], vecs, fake_embed), [q])
    row = res["rows"][0]
    assert row["hybrid"]["section_rank"] in (1, 2) and row["hybrid"]["doc_hit"]
    assert row["forbidden"] == ["faq/faq_old_copy.md"] and res["forbidden_returned"] == 1


def test_long_lists_split_and_keep_their_lead_in() -> None:
    body = ["Prices are typical ranges.", *[f"- item {i}" for i in range(14)]]
    split = pieces(body)
    assert [len(p) for p in split] == [7, 7, 3]
    assert all(p[0] == "Prices are typical ranges." for p in split)
    assert pieces(["Intro.", *[f"- x{i}" for i in range(10)]]) == [
        ["Intro.", *[f"- x{i}" for i in range(10)]]
    ]


def test_table_pieces_carry_the_surrounding_text() -> None:
    body = [
        "Before the table.",
        "| A | B |",
        "|---|---|",
        *[f"| {i} | v |" for i in range(8)],
        "After.",
    ]
    split = pieces(body)
    assert len(split) == 2
    for p in split:
        assert p[:4] == ["Before the table.", "After.", "| A | B |", "|---|---|"]


def test_price_book_caveat_is_indexed_and_stale_service_area_is_not() -> None:
    chunks = build_chunks()
    book = [c for c in chunks if c.doc_path == "pricing/repair_price_book_2026.csv"]
    assert len(book) >= 2 and all("technician confirms pricing on site" in c.text for c in book)
    faq = [c for c in chunks if c.doc_path == "faq/customer_faq.md"]
    assert faq and not any("Service area:" in c.text for c in faq)


def test_zero_query_vector_does_not_produce_nan() -> None:
    chunks = build_chunks()
    index = Index(chunks, fake_embed([c.text for c in chunks]), lambda ts: np.zeros((len(ts), 64)))
    zero = np.zeros(64)
    assert np.isfinite(index.vectors @ (zero / safe_norm(zero))).all()
    first = [h.chunk.id for h in index.search("anything", k=4, mode="embeddings")]
    assert first == [c.id for c in chunks[:4]]  # all scores 0 -> stable order, not NaN shuffle


def test_committed_results_match_the_questions_file() -> None:
    res = json.loads(Path("evals/retrieval/results/latest.json").read_text())
    qs = load_questions()
    assert [(r["id"], r["expected"], r["answer"]) for r in res["rows"]] == [
        (q["id"], sorted(q["expected"]), q.get("answer", [])) for q in qs
    ], "results are stale: re-run python -m evals.retrieval.run"
    assert res["forbidden_returned"] == 0
    hyb = [r["hybrid"]["section_rank"] for r in res["rows"] if r["scored"]]
    assert res["modes"]["hybrid"]["section_top3"] == sum(1 for x in hyb if x and x <= 3)
