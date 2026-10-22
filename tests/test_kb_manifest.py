import shutil
from pathlib import Path

import pytest

from kb.convert import csv_to_markdown, pdf_to_markdown
from meridian.kb.manifest import KB_DIR, MANIFEST, ManifestError, load, searchable

CONVERTED = Path(__file__).resolve().parents[1] / "kb/converted"


def test_manifest_lists_every_kb_file_and_loads() -> None:
    docs = load()
    assert len(docs) == 17


def test_unlisted_kb_file_fails_the_build(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb)
    (kb / "faq" / "new_promo.md").write_text("# Promo\n50% off everything!\n")
    with pytest.raises(ManifestError, match="not in the manifest"):
        load(MANIFEST, kb)


def test_listed_but_missing_file_fails(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb)
    (kb / "faq" / "customer_faq.md").unlink()
    with pytest.raises(ManifestError, match="not in the KB folder"):
        load(MANIFEST, kb)


def test_only_current_customer_docs_are_searchable() -> None:
    paths = {d.path for d in searchable(load())}
    for never in (
        "archive/comfort_club_overview.md",
        "archive/warranty_terms_2023.txt",
        "faq/faq_old_copy.md",
        "services/duct_cleaning.md",
        "internal/agent_discretion_notes.md",
        "policies/escalation_and_safety.md",
        "services/scheduling_guide.md",
    ):
        assert never not in paths
    assert "membership/comfort_club_overview.md" in paths
    assert "pricing/2026_fee_schedule.pdf" in paths


def test_non_text_docs_are_read_from_their_converted_text() -> None:
    by_path = {d.path: d for d in load()}
    for src in (
        "pricing/2026_fee_schedule.pdf",
        "policies/holiday_hours_2026.png",
        "pricing/repair_price_book_2026.csv",
    ):
        assert by_path[src].text_path.parent == CONVERTED


def test_converted_holiday_hours_match_the_image() -> None:
    """Verified by reading the PNG: every line below is in the image."""
    text = (CONVERTED / "holiday_hours_2026.md").read_text()
    for line in (
        "Memorial Day (May 25): Emergency only",
        "July 4: Closed. Emergency line open",
        "Labor Day (Sep 7): Emergency only",
        "Thanksgiving (Nov 26-27): Closed. Emergency line open",
        "Christmas Eve (Dec 24): 7am - 12pm",
        "Christmas Day (Dec 25): Closed. Emergency line open",
        "New Year's Day (Jan 1): Closed. Emergency line open",
        "Emergency dispatch fee on holidays: $199",
    ):
        assert line in text


def test_fee_table_keeps_member_columns() -> None:
    text = (CONVERTED / "2026_fee_schedule.md").read_text()
    assert "| Fee | Non-member | Comfort Club | Comfort Club Plus |" in text
    assert "| After-hours emergency dispatch | $149 | Waived | Waived |" in text
    assert "| Membership (monthly, 1 system) | — | $16.99 | Not offered |" in text


def test_pdf_to_markdown_builds_a_table_and_keeps_notes() -> None:
    layout = (
        "  Title line\n  continued\nEffective now.\n\n"
        "   Fee        Non-member   Club    Plus\n"
        "   Diagnostic   $89        Waived   Waived\n\n"
        "Notes: applies to residential.\n"
    )
    md = pdf_to_markdown(layout, "x.pdf")
    assert "| Diagnostic | $89 | Waived | Waived |" in md
    assert md.rstrip().endswith("Notes: applies to residential.")
    with pytest.raises(ValueError):
        pdf_to_markdown("no table here\n\n", "x.pdf")


def test_csv_to_markdown_one_line_per_item_with_ranges() -> None:
    rows = [
        {
            "sku": "A",
            "description": "Thing",
            "category": "Fee",
            "low_price": "10",
            "high_price": "20",
            "member_discount_eligible": "N",
            "notes": "",
        },
        {
            "sku": "B",
            "description": "Flat",
            "category": "Fee",
            "low_price": "5",
            "high_price": "5",
            "member_discount_eligible": "Y",
            "notes": "Waived for members",
        },
    ]
    md = csv_to_markdown(rows, "x.csv")
    assert "- Thing (Fee, SKU A): $10–$20. Member discount eligible: N." in md
    assert "- Flat (Fee, SKU B): $5. Member discount eligible: Y. Note: Waived for members." in md


def _write_manifest(tmp_path: Path, extra: str) -> Path:
    m = tmp_path / "manifest.toml"
    m.write_text(MANIFEST.read_text() + extra)
    return m


def test_customer_arrival_text_has_no_staff_waivers() -> None:
    by_path = {d.path: d for d in load()}
    text = by_path["services/technician_arrival_and_etas.md"].text_path.read_text()
    # skip the italic provenance note, which explains the removal
    body = "\n\n".join(p for p in text.split("\n\n") if not p.startswith("_"))
    assert "waive" not in body.lower()
    assert "A missed window is never charged as a no-show." in text


def test_non_text_file_without_conversion_fails(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb)
    (kb / "pricing" / "extra.csv").write_text("a,b\n1,2\n")
    m = _write_manifest(
        tmp_path,
        '\n[[doc]]\npath = "pricing/extra.csv"\nstatus = "current"\naudience = "customer"\n',
    )
    with pytest.raises(ManifestError, match="need a converted"):
        load(m, kb)


def test_superseded_by_must_be_a_current_doc(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb)
    (kb / "faq" / "older.md").write_text("old")
    m = _write_manifest(
        tmp_path,
        '\n[[doc]]\npath = "faq/older.md"\nstatus = "superseded"\naudience = "customer"\n'
        'superseded_by = "faq/faq_old_copy.md"\n',
    )
    with pytest.raises(ManifestError, match="not a listed current doc"):
        load(m, kb)


def test_missing_key_is_a_manifest_error(tmp_path: Path) -> None:
    m = _write_manifest(tmp_path, '\n[[doc]]\npath = "faq/customer_faq.md"\n')
    with pytest.raises(ManifestError, match="missing"):
        load(m, KB_DIR)
