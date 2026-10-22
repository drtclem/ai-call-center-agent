"""Cut current, customer-facing KB docs into searchable sections (D6).

One chunk per heading section. Every chunk starts with a context line (document title,
section heading, effective date) so a retrieved piece is self-explanatory. Tables are split
into groups of rows that each repeat the header row, so "Waived" stays tied to "Comfort Club".
Long bullet lists (the price book) are split into groups of lines. Italic provenance notes
(lines wrapped in underscores) are left out of the index.
"""

import hashlib
import re
from dataclasses import dataclass

from meridian.kb.manifest import Doc, load, searchable

TABLE_ROWS_PER_CHUNK = 6
LIST_LINES_PER_CHUNK = 6
LIST_SPLIT_ABOVE = 10  # short lists (e.g. holiday hours) stay whole
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


@dataclass(frozen=True)
class Chunk:
    id: str
    doc_path: str
    heading: str
    effective: str | None
    text: str

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:12]


def _is_provenance(line: str) -> bool:
    s = line.strip()
    return len(s) > 1 and s.startswith("_") and s.endswith("_")


def _sections(text: str) -> list[tuple[str, str, list[str]]]:
    """(doc title, section heading, body lines) for each heading section."""
    title = ""
    out: list[tuple[str, str, list[str]]] = []
    heading, body = "", list[str]()
    for line in text.splitlines():
        if _is_provenance(line):
            continue
        m = _HEADING.match(line)
        if m:
            if body and any(b.strip() for b in body):
                out.append((title, heading, body))
            level, name = len(m.group(1)), m.group(2).strip()
            if level == 1 and not title:
                title = name
            heading, body = ("" if level == 1 else name), []
        else:
            body.append(line)
    if body and any(b.strip() for b in body):
        out.append((title, heading, body))
    return out


def pieces(body: list[str]) -> list[list[str]]:
    """Split a section body into chunk-sized pieces, repeating table headers."""
    table = [ln for ln in body if ln.strip().startswith("|")]
    other = [ln for ln in body if not ln.strip().startswith("|") and ln.strip()]
    if len(table) > 2 + TABLE_ROWS_PER_CHUNK:
        header, rows = table[:2], table[2:]
        groups = [
            rows[i : i + TABLE_ROWS_PER_CHUNK] for i in range(0, len(rows), TABLE_ROWS_PER_CHUNK)
        ]
        return [other + header + g for g in groups]
    bullets = [ln for ln in other if ln.lstrip().startswith("- ")]
    if not table and len(bullets) > LIST_SPLIT_ABOVE:
        # shortcut: nested bullets count as lines, so a long nested list could split from its parent
        lead = [ln for ln in other if not ln.lstrip().startswith("- ")]
        return [
            lead + bullets[i : i + LIST_LINES_PER_CHUNK]
            for i in range(0, len(bullets), LIST_LINES_PER_CHUNK)
        ]
    return [[ln for ln in body if ln.strip()]]


def chunk_doc(doc: Doc) -> list[Chunk]:
    text = doc.text_path.read_text()
    chunks: list[Chunk] = []
    for title, heading, body in _sections(text):
        for i, piece in enumerate(pieces(body)):
            where = f"{title} — {heading}" if heading else title
            eff = f" (effective {doc.effective})" if doc.effective else ""
            ctx = f"[{where}{eff}]"
            slug = re.sub(r"[^a-z0-9]+", "-", (heading or "intro").lower()).strip("-")
            chunks.append(
                Chunk(
                    id=f"{doc.path}#{slug}-{i}",
                    doc_path=doc.path,
                    heading=heading or title,
                    effective=doc.effective,
                    text=ctx + "\n" + "\n".join(piece),
                )
            )
    return chunks


def build_chunks() -> list[Chunk]:
    """All chunks of all searchable docs, in manifest order."""
    return [c for d in searchable(load()) for c in chunk_doc(d)]
