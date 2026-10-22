"""R-34: production turns off FastAPI docs; /health stays up (D20, T0)."""

import datetime
import pathlib
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meridian.config import Settings
from meridian.web.app import create_app

DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
DATASET_TODAY = datetime.date(2026, 10, 16)
_TMP_DB = pathlib.Path(tempfile.mkdtemp(prefix="meridian-health-")) / "m.sqlite"


def _response_status(path: str, settings: Settings | None) -> int:
    app = create_app(settings)
    assert isinstance(app, FastAPI)
    with TestClient(app) as client:
        return client.get(path).status_code


def test_r34_health_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("APP_TODAY", raising=False)
    app = create_app()
    assert isinstance(app, FastAPI)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


def test_r34_health_returns_ok_in_production() -> None:
    settings = Settings(env="production", app_today=DATASET_TODAY, db_path=_TMP_DB)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_r34_production_settings_disable_docs(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """Explicit Settings control the app, even when the process ENV is development."""
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("APP_TODAY", raising=False)
    settings = Settings(env="production", app_today=DATASET_TODAY, db_path=_TMP_DB)

    assert _response_status(path, settings) == 404


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_r34_development_settings_enable_docs(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setenv("ENV", "production")
    settings = Settings(env="development", app_today=DATASET_TODAY, db_path=_TMP_DB)

    assert _response_status(path, settings) == 200


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_r34_production_env_disables_docs_when_settings_omitted(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("APP_TODAY", "2026-10-16")

    assert _response_status(path, None) == 404


def test_deep_health_checks_db_and_model_and_caches_the_model_call(tmp_path: pathlib.Path) -> None:
    """D15.6 / T18: the hourly alert hits /health?deep=1."""
    calls: list[int] = []
    up = [True]

    def probe() -> bool:
        calls.append(1)
        return up[0]

    settings = Settings(env="development", app_today=DATASET_TODAY, db_path=tmp_path / "m.sqlite")
    with TestClient(create_app(settings, model_check=probe)) as client:
        ok = client.get("/health?deep=1")
        assert ok.status_code == 200 and ok.json() == {"status": "ok", "db": "ok", "model": "ok"}
        up[0] = False
        assert client.get("/health?deep=1").status_code == 200  # cached for 5 minutes
        assert len(calls) == 1
        assert client.get("/health").json() == {"status": "ok"}  # liveness never calls out
        assert len(calls) == 1


def test_deep_health_fails_when_the_model_is_unreachable(tmp_path: pathlib.Path) -> None:
    settings = Settings(env="development", app_today=DATASET_TODAY, db_path=tmp_path / "m.sqlite")
    with TestClient(create_app(settings, model_check=lambda: False)) as client:
        bad = client.get("/health?deep=1")
    assert bad.status_code == 503 and bad.json()["model"] == "failed"


def test_concurrent_deep_checks_call_the_provider_once(tmp_path: pathlib.Path) -> None:
    """T18 review: a flood after the cache expires must not fan out to the provider."""
    import threading
    import time

    calls: list[int] = []

    def slow() -> bool:
        calls.append(1)
        time.sleep(0.3)
        return True

    settings = Settings(env="development", app_today=DATASET_TODAY, db_path=tmp_path / "m.sqlite")
    with TestClient(create_app(settings, model_check=slow)) as client:
        threads = [threading.Thread(target=client.get, args=("/health?deep=1",)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(calls) == 1
