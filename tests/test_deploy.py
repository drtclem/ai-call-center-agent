"""T17: what the deploy image needs, checked without Docker; and no caller text in error logs."""

import logging
import re
from pathlib import Path

import pytest

from meridian.config import ROOT
from meridian.db import DATASET_DB
from meridian.kb.manifest import KB_DIR, MANIFEST
from meridian.kb.search import INDEX_DIR
from meridian.web.app import NoClientIP, NoExceptionText, _quiet_exceptions  # pyright: ignore[reportPrivateUsage]


def _copied() -> set[str]:
    text = (ROOT / "Dockerfile").read_text()
    return {m.group(1) for m in re.finditer(r"^COPY\s+(?!--)(\S+)", text, re.M)}


@pytest.mark.parametrize("path", [DATASET_DB, KB_DIR, MANIFEST, INDEX_DIR, ROOT / "kb/converted"])
def test_the_image_gets_every_runtime_file(path: Path) -> None:
    rel = path.relative_to(ROOT).as_posix()
    assert any(rel == c or rel.startswith(c.rstrip("/") + "/") for c in _copied()), rel


def test_dockerignore_lets_the_kb_through() -> None:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert "!data/meridian_dataset/knowledge_base" in lines


def test_entrypoint_drops_root() -> None:
    script = (ROOT / "scripts/entrypoint.sh").read_text()
    assert "setpriv --reuid=10001" in script and script.rstrip().endswith('-- "$@"')
    assert (ROOT / "scripts/entrypoint.sh").stat().st_mode & 0o111


def test_uvicorn_error_logs_keep_the_type_not_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _quiet_exceptions()
    _quiet_exceptions()  # idempotent
    logger = logging.getLogger("uvicorn.error")
    assert sum(isinstance(f, NoExceptionText) for f in logger.filters) == 1
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        try:
            said = "512-555-" + "0101"  # caller text arrives at runtime, never in source lines
            raise ValueError(f"caller said {said}")
        except ValueError:
            logger.exception("Exception in ASGI application")
    assert "ValueError" in caplog.text and "test_deploy.py" in caplog.text
    assert "512-555-0101" not in caplog.text


def test_no_logger_call_attaches_exception_text() -> None:
    """Only uvicorn's logger is filtered; our own code must never log exc_info (D17, T17)."""
    for f in (ROOT / "src").rglob("*.py"):
        text = f.read_text()
        assert "exc_info=True" not in text and ".exception(" not in text, f


def test_entrypoint_refuses_a_db_path_in_the_app_tree() -> None:
    assert "/app | /app/*)" in (ROOT / "scripts/entrypoint.sh").read_text()


def test_access_log_keeps_the_request_but_not_the_ip(caplog: pytest.LogCaptureFixture) -> None:
    _quiet_exceptions()
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, NoClientIP) for f in access.filters) == 1
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info('%s - "%s %s HTTP/%s" %d', "12.79.229.62:0", "POST", "/chat", "1.1", 200)
    assert '- - "POST /chat HTTP/1.1" 200' in caplog.text
    assert "12.79.229.62" not in caplog.text
