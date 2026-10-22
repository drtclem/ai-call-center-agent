"""Runtime database (D13, architecture §4).

The app never touches the dataset file (rule 1). On first boot it copies it to `DB_PATH`
(the Railway volume in production), then adds the app tables and the canary customers (D20).
Later boots reuse the copy, so bookings survive restarts and deploys.
"""

import os
import sqlite3
from pathlib import Path

from meridian.config import ROOT, get_settings

DATASET_DB = ROOT / "data/meridian_dataset/scheduling_db/meridian_scheduling.sqlite"

# Migrations (architecture §4), applied in order above PRAGMA user_version, each in one
# transaction with its version bump. Never edit a released step; append a new one.
# Customer money is integer cents; our model spend is integer micro-dollars (rule 16).
_V1 = """
BEGIN;
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  visitor_key TEXT NOT NULL,
  customer_id TEXT REFERENCES customers(customer_id),
  outcome TEXT,
  outcome_reason TEXT,
  rating INTEGER CHECK (rating IN (-1, 1)),
  turns INTEGER NOT NULL DEFAULT 0,
  cost_usd_micros INTEGER NOT NULL DEFAULT 0,
  models_used TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS turn_events (
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  worker TEXT,
  tool_calls TEXT NOT NULL DEFAULT '[]',
  sections TEXT NOT NULL DEFAULT '[]',
  check_result TEXT,
  latency_ms INTEGER,
  model TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  cost_usd_micros INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS handoff_tickets (
  id INTEGER PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  reason TEXT NOT NULL,
  urgency TEXT NOT NULL,
  customer_id TEXT REFERENCES customers(customer_id),
  verified INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  tried TEXT,
  wants TEXT,
  callback_window TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  resolution_code TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fee_reviews (
  id INTEGER PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  appointment_id INTEGER NOT NULL REFERENCES appointments(appointment_id),
  change TEXT NOT NULL CHECK (change IN ('reschedule', 'cancel')),
  amount_cents INTEGER NOT NULL,
  consent_text TEXT NOT NULL,
  consent_method TEXT NOT NULL CHECK (consent_method IN ('button', 'typed')),
  consent_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS flags (
  id INTEGER PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL,
  review_tag TEXT,
  correct_answer TEXT,
  reviewed_by TEXT,
  added_as_test INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_daily (
  date TEXT PRIMARY KEY,
  cost_usd_micros INTEGER NOT NULL DEFAULT 0,
  conversations INTEGER NOT NULL DEFAULT 0,
  limit_hits INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rate_events (
  visitor_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_events_by_visitor ON rate_events (visitor_key, kind, at);
PRAGMA user_version = 1;
COMMIT;
"""
# v2 (T8 review): keep the exact fee wording the caller agreed to (D10.8).
_V2 = """
BEGIN;
ALTER TABLE fee_reviews ADD COLUMN notice_text TEXT;
PRAGMA user_version = 2;
COMMIT;
"""
# v3 (T19): one flag per conversation and kind, so re-running a trigger never duplicates, and
# the reviewed test case stored on its flag (the container's disk doesn't survive deploys; D16).
_V3 = """
BEGIN;
DELETE FROM flags WHERE id NOT IN (SELECT min(id) FROM flags GROUP BY conversation_id, kind);
ALTER TABLE flags ADD COLUMN test_case TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS flags_once ON flags (conversation_id, kind);
PRAGMA user_version = 3;
COMMIT;
"""
MIGRATIONS: tuple[str, ...] = (_V1, _V2, _V3)  # step i sets user_version = i + 1
SCHEMA_VERSION = len(MIGRATIONS)

# Canary customers (D20): fake records with unique markers. A marker in a reply to anyone not
# verified as that customer is a leak, flagged in code (T11/T16). Runtime copy only, never the
# dataset. One per zone so live checks can exercise each zone's slots.
CANARIES = (
    ("MHC-00901", "Wren Halvorsen", "512-555-0191", "wren.h.CANARY-7Q2@example.com",
     "88 Larkspur Bend Unit CANARY-7Q2", "Round Rock", "78664", "standard", "2027-03-01",
     "Trane", "2019-05-02"),
    ("MHC-00902", "Idris Calloway", "512-555-0192", "idris.c.CANARY-K4M@example.com",
     "1207 Sable Creek Dr Unit CANARY-K4M", "Austin", "78745", "none", None,
     "Carrier", "2016-08-19"),
    ("MHC-00903", "Noemi Varga", "512-555-0193", "noemi.v.CANARY-P9X@example.com",
     "46 Juniper Hollow Unit CANARY-P9X", "Kyle", "78640", "plus", "2027-01-15",
     "Lennox", "2021-02-11"),
)  # fmt: skip
# Fields carrying the marker (what T11/T16 scan for): email and service_address.
_CANARY_INSERT = (
    "INSERT OR IGNORE INTO customers (customer_id, name, phone, email, service_address, city, "
    "zip, membership_tier, membership_renewal_date, system_brand, system_install_date) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
CANARY_MARKERS = ("CANARY-7Q2", "CANARY-K4M", "CANARY-P9X")


def db_path() -> Path:
    """Runtime DB location: `DB_PATH` (the Railway volume) or `var/meridian.sqlite` locally."""
    return get_settings().db_path


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the runtime DB (never the dataset file). Callers use `with conn:` for transactions."""
    p = path or db_path()
    if p.resolve() == DATASET_DB.resolve():
        raise ValueError("refusing to open the dataset file for writing (rule 1)")
    conn = sqlite3.connect(p, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | None = None, source: Path = DATASET_DB) -> Path:
    """First boot: copy the dataset DB. Every boot: pending migrations, then canaries."""
    p = path or db_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.unlink(missing_ok=True)
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)  # validates the source; the file is complete and synced when it returns
        finally:
            src.close()
            dst.close()
        os.replace(tmp, p)  # atomic: a crash mid-copy never leaves a half file at DB_PATH
    conn = connect(p)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for step in MIGRATIONS[version:]:
            conn.executescript(
                step
            )  # a failed step leaves its transaction open; close() rolls back
        with conn:
            conn.executemany(_CANARY_INSERT, CANARIES)
    finally:
        conn.close()
    return p


MERIDIAN_TABLES = ("technicians", "customers", "appointment_slots", "appointments", "invoices")


def reset_demo_data(conn: sqlite3.Connection, source: Path = DATASET_DB) -> None:
    """Staff action (D17.8): put Meridian's tables back as given, plus the canaries. Fee reviews
    point at bookings that may disappear, so they go too; conversation logs are kept."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)  # the dataset, read-only
    try:
        rows = {t: src.execute(f"SELECT * FROM {t}").fetchall() for t in MERIDIAN_TABLES}  # noqa: S608
    finally:
        src.close()
    conn.execute("BEGIN IMMEDIATE")  # explicit: the deferral below only lasts one transaction
    try:
        conn.execute("PRAGMA defer_foreign_keys = ON")  # logs point at customers being re-added
        conn.execute("DELETE FROM fee_reviews")
        for table in reversed(MERIDIAN_TABLES):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608  (constant names)
        for table, data in rows.items():
            if data:
                marks = ", ".join("?" * len(data[0]))
                conn.executemany(f"INSERT INTO {table} VALUES ({marks})", data)  # noqa: S608
        conn.executemany(_CANARY_INSERT, CANARIES)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def delete_conversation_logs(conn: sqlite3.Connection) -> None:
    """Staff action (D17.8): remove conversation text, tickets, fee reviews and flags."""
    with conn:
        for table in ("flags", "fee_reviews", "handoff_tickets", "turn_events", "conversations"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608  (constant names)
