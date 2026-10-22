import sqlite3
from pathlib import Path

import pytest

from meridian.config import get_settings
from meridian.db import (
    CANARIES,
    CANARY_MARKERS,
    DATASET_DB,
    MIGRATIONS,
    SCHEMA_VERSION,
    connect,
    init_db,
)
from meridian.scheduling.zones import zone_for


def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def test_first_boot_copies_and_second_boot_keeps_bookings(tmp_path: Path) -> None:
    p = tmp_path / "vol" / "meridian.sqlite"
    init_db(p)
    with connect(p) as c:
        c.execute("UPDATE appointment_slots SET status = 'booked' WHERE slot_id = 1")
    init_db(p)  # restart / redeploy
    with connect(p) as c:
        assert (
            c.execute("SELECT status FROM appointment_slots WHERE slot_id = 1").fetchone()[0]
            == "booked"
        )
    assert not (tmp_path / "vol" / "meridian.sqlite.tmp").exists()


def test_copy_has_dataset_rows_app_tables_and_canaries(db: Path) -> None:
    with connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {
            "conversations",
            "turn_events",
            "handoff_tickets",
            "fee_reviews",
            "flags",
            "usage_daily",
            "rate_events",
            "customers",
            "appointment_slots",
        } <= tables
        n = c.execute("SELECT count(*) FROM customers").fetchone()[0]
        assert n == 154 + len(CANARIES)
    with _ro(DATASET_DB) as d:
        assert d.execute("SELECT count(*) FROM customers").fetchone()[0] == 154
        assert (
            d.execute(
                "SELECT count(*) FROM customers WHERE customer_id LIKE 'MHC-009%'"
            ).fetchone()[0]
            == 0
        )


def test_canaries_carry_their_marker_and_a_bookable_zone() -> None:
    for row, marker in zip(CANARIES, CANARY_MARKERS, strict=True):
        assert marker in row[3] and marker in row[4]
        assert zone_for(row[5]) is not None


def test_connect_refuses_the_dataset_file() -> None:
    with pytest.raises(ValueError, match="dataset"):
        connect(DATASET_DB)


def test_every_customer_and_slot_zone_is_known(db: Path) -> None:
    with connect(db) as c:
        for (city,) in c.execute("SELECT DISTINCT city FROM customers"):
            assert zone_for(city) is not None, city
        assert {r[0] for r in c.execute("SELECT DISTINCT zone FROM appointment_slots")} == {
            "north",
            "central",
            "south",
        }
    assert zone_for(" Round Rock ") == "north"
    assert zone_for("Manor") is None and zone_for("Dripping Springs") is None


def test_migrations_apply_only_the_missing_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = init_db(tmp_path / "m.sqlite")
    nxt = SCHEMA_VERSION + 1
    v2 = f"BEGIN;\nCREATE TABLE extra (x INTEGER);\nPRAGMA user_version = {nxt};\nCOMMIT;\n"
    monkeypatch.setattr("meridian.db.MIGRATIONS", (*MIGRATIONS, v2))
    init_db(p)  # an existing volume DB gets only step 2
    init_db(p)  # and nothing on the next boot
    with connect(p) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == nxt
        assert c.execute("SELECT count(*) FROM extra").fetchone()[0] == 0


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = init_db(tmp_path / "m.sqlite")
    bad = (
        "BEGIN;\nCREATE TABLE half (x INTEGER);\nSELECT * FROM no_such_table;\n"
        f"PRAGMA user_version = {SCHEMA_VERSION + 1};\nCOMMIT;\n"
    )
    monkeypatch.setattr("meridian.db.MIGRATIONS", (*MIGRATIONS, bad))
    with pytest.raises(sqlite3.OperationalError):
        init_db(p)
    with connect(p) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'half'").fetchone()


def test_canary_members_look_like_real_members(db: Path) -> None:
    with connect(db) as c:
        rows = c.execute(
            "SELECT membership_tier, membership_renewal_date, system_brand FROM customers "
            "WHERE customer_id LIKE 'MHC-009%'"
        ).fetchall()
    for tier, renewal, brand in rows:
        assert brand and ((tier == "none") == (renewal is None))
        assert renewal is None or renewal > "2026-10-16"


def test_production_requires_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("DB_PATH")
    with pytest.raises(ValueError, match="DB_PATH"):
        get_settings()
