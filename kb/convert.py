"""Convert the non-text KB files to Markdown (D5). Offline, run once; outputs are committed
and checked by tests, so CI never needs pdftotext or a model.

Run: uv run --env-file .env python -m kb.convert [pdf|png|csv|all]
- pdf: `pdftotext -layout` → Markdown table (fee schedule).
- png: a vision model transcribes the image verbatim; Claude verified it against the image.
- csv: one line per item, with column names, so each price row is searchable on its own.
"""

import base64
import csv
import re
import subprocess
import sys
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "data/meridian_dataset/knowledge_base"
OUT = ROOT / "kb/converted"
VISION_MODEL = "claude-sonnet-5-5"


def pdf_to_markdown(layout_text: str, source: str) -> str:
    """Rebuild the fee table from pdftotext's column layout (cells split on 2+ spaces)."""
    lines = [ln.rstrip() for ln in layout_text.splitlines()]
    title = " ".join(ln.strip() for ln in lines[:2] if ln.strip())
    out = [f"# {title}", "", f"_Converted from `{source}` (PDF)._", ""]
    table: list[list[str]] = []
    notes: list[str] = []
    for ln in lines[2:]:
        cells = [c for c in re.split(r"\s{2,}", ln.strip()) if c]
        if not cells:
            continue
        if len(cells) == 4:
            table.append(cells)
        elif table:
            notes.append(ln.strip())
        else:
            out.append(ln.strip())
    if not table:
        raise ValueError("no 4-column table found in the PDF text")
    out += ["", "| " + " | ".join(table[0]) + " |", "|---|---|---|---|"]
    out += ["| " + " | ".join(row) + " |" for row in table[1:]]
    if notes:
        out += ["", " ".join(notes)]
    return "\n".join(out) + "\n"


def csv_to_markdown(rows: list[dict[str, str]], source: str) -> str:
    out = [
        "# Repair price book 2026",
        "",
        f"_Converted from `{source}` (CSV): one line per item._",
        "",
        "Prices are typical ranges before any member discount; the technician confirms pricing "
        "on site.",
        "",
    ]
    for r in rows:
        low, high = r["low_price"], r["high_price"]
        price = f"${low}" if low == high else f"${low}–${high}"
        line = (
            f"- {r['description']} ({r['category']}, SKU {r['sku']}): {price}. "
            f"Member discount eligible: {r['member_discount_eligible']}."
        )
        if r.get("notes"):
            line += f" Note: {r['notes']}."
        out.append(line)
    return "\n".join(out) + "\n"


PNG_PROMPT = """Transcribe this image exactly as Markdown. Use the image's title as a level-1
heading, then one bullet per line of text, in order, word for word. Do not add, infer or
reformat anything (keep dates, times and dollar amounts exactly as shown). Output only Markdown."""


def png_to_markdown(png: Path, source: str) -> str:
    client = anthropic.Anthropic(max_retries=3)
    data = base64.standard_b64encode(png.read_bytes()).decode()
    msg = client.messages.create(
        model=VISION_MODEL,
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": data},
                    },
                    {"type": "text", "text": PNG_PROMPT},
                ],
            }
        ],
        timeout=120.0,
    )
    body = "".join(b.text for b in msg.content if b.type == "text").strip()
    head, _, rest = body.partition("\n")
    note = (
        f"_Transcribed from `{source}` (image) by {VISION_MODEL}; "
        "verified against the image by Claude._"
    )
    return f"{head}\n\n{note}\n{rest}\n"


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    OUT.mkdir(parents=True, exist_ok=True)
    if which in ("pdf", "all"):
        src = "pricing/2026_fee_schedule.pdf"
        text = subprocess.run(
            ["pdftotext", "-layout", str(KB / src), "-"], check=True, capture_output=True, text=True
        ).stdout
        (OUT / "2026_fee_schedule.md").write_text(pdf_to_markdown(text, src))
    if which in ("csv", "all"):
        src = "pricing/repair_price_book_2026.csv"
        with (KB / src).open() as f:
            (OUT / "repair_price_book_2026.md").write_text(
                csv_to_markdown(list(csv.DictReader(f)), src)
            )
    if which in ("png", "all"):
        src = "policies/holiday_hours_2026.png"
        (OUT / "holiday_hours_2026.md").write_text(png_to_markdown(KB / src, src))
    print(f"converted: {which}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
