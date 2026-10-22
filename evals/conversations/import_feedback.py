"""Export reviewed feedback cases into the eval dev area (D16.4, D16.8). Run locally:

  python -m evals.conversations.import_feedback var/meridian.sqlite
  python -m evals.conversations.import_feedback ~/Downloads/feedback-<conversation>-<kind>.json

The argument is a runtime DB copy (every flag with a stored case) or one case downloaded from
the staff flags page. Files go to evals/conversations/dev/ through the dev-only guard; an
existing file is kept, never overwritten.
"""

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from meridian.feedback import write_dev_case


def read_cases(source: Path) -> list[dict[str, Any]]:
    if source.suffix == ".json":
        return [json.loads(source.read_text())]
    db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)  # never writes the source
    try:
        rows = db.execute("SELECT test_case FROM flags WHERE test_case IS NOT NULL").fetchall()
    finally:
        db.close()
    return [json.loads(r[0]) for r in rows]


def main(argv: list[str] | None = None, dest: Path | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    written = 0
    for case in read_cases(Path(args[0])):
        try:
            print(f"wrote {write_dev_case(case, dest)}")
            written += 1
        except FileExistsError as exc:
            print(f"kept existing {exc.filename}")
        except ValueError as exc:
            print(f"skipped {case.get('id', '?')}: {exc}", file=sys.stderr)
    print(f"{written} new case(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
