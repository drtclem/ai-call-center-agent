import sqlite3

import pytest

from discovery.label import check_label
from discovery.report import caller_city, cohen_kappa

IDS = {"repair", "billing", "other"}


def test_kappa_perfect_chance_and_errors() -> None:
    assert cohen_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    # Same marginals, no agreement beyond chance on a balanced pair -> negative/zero kappa
    assert cohen_kappa(["a", "b"], ["b", "a"]) == -1.0
    assert cohen_kappa(["a", "a"], ["a", "a"]) == 1.0  # degenerate: one class only
    with pytest.raises(ValueError):
        cohen_kappa([], [])


def test_kappa_known_value() -> None:
    # 2x2 example: observed 0.7, expected 0.5 -> kappa 0.4
    a = ["y"] * 5 + ["n"] * 5
    b = ["y"] * 4 + ["n"] + ["y"] * 2 + ["n"] * 3
    assert cohen_kappa(a, b) == pytest.approx(0.4)


def test_check_label_rejects_unknown_primary_and_drops_bad_secondary() -> None:
    assert check_label({"primary": "repair", "secondary": "repair"}, IDS)["secondary"] is None
    assert check_label({"primary": "repair", "secondary": "nope"}, IDS)["secondary"] is None
    assert check_label({"primary": "repair", "secondary": "null"}, IDS)["secondary"] is None
    assert check_label({"primary": "repair", "secondary": "billing"}, IDS)["secondary"] == "billing"
    with pytest.raises(ValueError):
        check_label({"primary": "made_up"}, IDS)


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE customers (customer_id TEXT, phone TEXT, city TEXT)")
    con.executemany(
        "INSERT INTO customers VALUES (?, ?, ?)",
        [("MHC-10001", "512-555-0101", "Round Rock"), ("MHC-10002", "512-555-0102", "Austin")],
    )
    return con


def test_caller_city_by_account_phone_or_address() -> None:
    con = _db()
    assert caller_city("Customer: Yeah, it's MHC-10002.", con) == "Austin"
    assert caller_city("Customer: Omar, 512-555-0101.", con) == "Round Rock"
    assert caller_city("Customer: Aisha Diaz, 85 Cedar Ln, Austin.", con) == "Austin"
    assert caller_city("Customer: I'd rather not say.", con) is None


def test_report_numbers_match_labels_csv() -> None:
    """R-02/R-03: every headline number in report.md is recomputed from labels.csv."""
    import csv
    import re
    from collections import Counter
    from pathlib import Path

    rows = list(csv.DictReader(Path("discovery/labels.csv").open()))
    report = Path("discovery/report.md").read_text()
    assert len(rows) == 420
    agree = sum(r["opus"] == r["jev"] for r in rows)
    kappa = cohen_kappa([r["opus"] for r in rows], [r["jev"] for r in rows])
    assert f"**{agree}/420 ({agree / 420:.0%})**" in report
    assert f"kappa **{kappa:.3f}**" in report
    for bucket, n in Counter(r["final"] for r in rows).items():
        assert re.search(rf"\(`{bucket}`\) \| {n} \| {n / 420:.1%} \|", report), bucket
    minutes = sum(int(r["duration_sec"]) for r in rows) / 60
    assert f"(total {minutes:.0f})" in report
