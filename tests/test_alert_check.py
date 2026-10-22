"""T18: the scheduled alert script reads replies and health the way the live app sends them."""

import importlib.util
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType

import pytest

from meridian.config import ROOT


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("alert_check", ROOT / "scripts/alert_check.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ac = _load()
REPLY = {"body": "", "health": 200}


class _Fake(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(REPLY["health"])  # type: ignore[arg-type]
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        assert self.headers["Origin"].startswith("http://127.0.0.1")  # passes the CSRF check
        self.send_response(200)
        self.end_headers()
        self.wfile.write(str(REPLY["body"]).encode())

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def base() -> Iterator[str]:
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_health(base: str) -> None:
    REPLY["health"] = 200
    assert ac.health(base) == []
    REPLY["health"] = 503
    assert ac.health(base)


def test_conversation_checks_words_and_handoffs(base: str) -> None:
    REPLY["body"] = "<p>The diagnostic visit is $89.</p>"
    assert ac.conversation(base, ac.KB) == []
    REPLY["body"] = "<p>I'm not sure.</p>"
    assert ac.conversation(base, ac.KB)
    REPLY["body"] = '<div class="card ticket" aria-label="Handoff ticket">$89</div>'
    assert ac.conversation(base, ac.KB)


def test_the_ticket_marker_matches_the_template() -> None:
    html = (ROOT / "src/meridian/web/templates/_messages.html").read_text()
    assert 'aria-label="Handoff ticket"' in html


def test_cli_usage(tmp_path: Path) -> None:
    assert ac.main(["x"]) == 2
