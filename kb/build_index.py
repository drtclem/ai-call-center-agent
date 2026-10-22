"""Build the committed KB search index (D6). Offline; re-run whenever the KB or chunking changes.

Run: uv run --env-file .env python -m kb.build_index
"""

import sys

from meridian.agent.llm import embed_texts
from meridian.kb.chunk import build_chunks
from meridian.kb.search import save_index


def main() -> int:
    chunks = build_chunks()
    vectors = embed_texts([c.text for c in chunks])
    save_index(chunks, vectors)
    print(f"indexed {len(chunks)} chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
