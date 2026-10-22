"""KB manifest (D5): which knowledge-base files exist, which are current, which are searchable.

The build fails if any file in the KB folder is missing from the manifest, so a new or changed
document can't slip into the agent's answers unreviewed.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from meridian.config import ROOT  # re-exported for kb modules

KB_DIR = ROOT / "data/meridian_dataset/knowledge_base"
MANIFEST = ROOT / "kb/manifest.toml"
STATUSES = {"current", "superseded", "stub"}
AUDIENCES = {"customer", "agent_only"}


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Doc:
    path: str
    status: str
    audience: str
    effective: str | None
    text_path: Path

    @property
    def searchable(self) -> bool:
        return self.status == "current" and self.audience == "customer"


def load(manifest: Path = MANIFEST, kb_dir: Path = KB_DIR) -> list[Doc]:
    """Parse and validate the manifest against the files actually on disk."""
    raw = tomllib.loads(manifest.read_text())
    docs: list[Doc] = []
    superseded_by: dict[str, str] = {}
    for entry in raw.get("doc", []):
        try:
            path = str(entry["path"])
            status, audience = str(entry["status"]), str(entry["audience"])
        except KeyError as exc:
            raise ManifestError(f"manifest entry missing {exc}") from exc
        if status not in STATUSES or audience not in AUDIENCES:
            raise ManifestError(f"{path}: bad status or audience")
        if not (kb_dir / path).is_file():
            raise ManifestError(f"{path}: listed in the manifest but not in the KB folder")
        text = entry.get("text")
        if not text and Path(path).suffix not in (".md", ".txt"):
            raise ManifestError(f"{path}: non-text files need a converted `text`")
        text_path = ROOT / str(text) if text else kb_dir / path
        if status == "current" and not text_path.is_file():
            raise ManifestError(f"{path}: converted text {text} is missing")
        if status == "superseded":
            if "superseded_by" not in entry:
                raise ManifestError(f"{path}: superseded docs must name what replaced them")
            superseded_by[path] = str(entry["superseded_by"])
        eff = entry.get("effective")
        docs.append(Doc(path, status, audience, str(eff) if eff else None, text_path))
    listed = {d.path for d in docs}
    if len(listed) != len(docs):
        raise ManifestError("a KB file is listed twice")
    on_disk = {p.relative_to(kb_dir).as_posix() for p in kb_dir.rglob("*") if p.is_file()}
    unlisted = sorted(on_disk - listed)
    if unlisted:
        raise ManifestError(f"KB files not in the manifest: {unlisted}")
    current = {d.path for d in docs if d.status == "current"}
    for old, new in superseded_by.items():
        if new not in current:
            raise ManifestError(f"{old}: superseded_by {new} is not a listed current doc")
    return docs


def searchable(docs: list[Doc]) -> list[Doc]:
    return [d for d in docs if d.searchable]
