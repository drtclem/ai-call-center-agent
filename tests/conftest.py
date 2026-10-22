import hashlib
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from agent_fakes import INDEX, FakeLLM
from meridian import clock
from meridian.agent.graph import Agent
from meridian.agent.state import Conversation
from meridian.config import ROOT
from meridian.db import DATASET_DB, connect, init_db


def pytest_configure(config: pytest.Config) -> None:
    """Tests never create the runtime DB inside the repo (the app's startup calls init_db)."""
    os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="meridian-test-")) / "meridian.sqlite")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(scope="session", autouse=True)
def dataset_db_is_never_modified() -> Iterator[None]:
    """R-31 / rule 1: the dataset DB has the same bytes after the whole test run, and no test
    created the repo's own runtime DB (tests use temp copies)."""
    before = _sha(DATASET_DB)
    local = ROOT / "var/meridian.sqlite"
    existed = local.exists()
    yield
    assert _sha(DATASET_DB) == before, "a test modified the dataset DB"
    assert existed or not local.exists(), "a test created var/meridian.sqlite in the repo"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A fresh runtime DB copy per test."""
    return init_db(tmp_path / "meridian.sqlite")


@pytest.fixture
def env(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Agent, FakeLLM, Conversation, Path]]:
    monkeypatch.setenv("APP_TODAY", "2026-10-16")
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 16, 10, 0))
    llm = FakeLLM()
    agent = Agent(lambda: connect(db), llm, lambda q: INDEX.search(q))  # type: ignore[arg-type]
    yield agent, llm, agent.start("conv-1", "visitor-1"), db
    assert not llm.steps, f"scripted model steps never used: {llm.steps}"
