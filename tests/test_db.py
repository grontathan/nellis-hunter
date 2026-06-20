"""Tests for the SQLite layer — the new per-run `surfaced` flag, the `run_summary`
read-back, and the additive migration that adds the column to an older DB."""

import sqlite3
from datetime import datetime, timezone

from nellis_hunter.db import NellisDB
from nellis_hunter.models import Lot, Scoring, ScoredLot, Verdict


def _scored(lot_id: str, verdict: Verdict = Verdict.BID, margin: float = 30.0) -> ScoredLot:
    lot = Lot(
        lot_id=lot_id,
        title=f"Lot {lot_id}",
        location="Mesa",
        url=f"https://www.nellisauction.com/p/x/{lot_id}",
        current_bid=40.0,
        close_time=datetime(2026, 6, 20, 2, 0, tzinfo=timezone.utc),
    )
    sc = Scoring(verdict=verdict, max_bid=120.0, projected_margin_at_current_bid=margin)
    return ScoredLot(lot=lot, scoring=sc)


def test_record_run_persists_surfaced_flag(tmp_path):
    db = NellisDB(tmp_path / "t.db")
    scored = [_scored("1"), _scored("2", Verdict.WATCH), _scored("3", Verdict.SKIP)]
    surfaced = [scored[0]]  # only lot 1 made the digest
    db.record_run(scored, surfaced=surfaced)

    rows = {r["lot_id"]: r["surfaced"] for r in db.conn.execute("SELECT lot_id, surfaced FROM scored")}
    assert rows == {"1": 1, "2": 0, "3": 0}
    db.close()


def test_run_summary_exact_surfaced_count(tmp_path):
    db = NellisDB(tmp_path / "t.db")
    scored = [_scored(str(i), Verdict.BID if i < 5 else Verdict.WATCH) for i in range(10)]
    db.record_run(scored, surfaced=scored[:3])

    summary = db.run_summary()
    assert summary["scanned"] == 10
    assert summary["surfaced"] == 3  # exact column read, not a cap-bound estimate
    assert summary["verdicts"] == {"BID": 5, "WATCH": 5}
    db.close()


def test_run_summary_empty_db(tmp_path):
    db = NellisDB(tmp_path / "empty.db")
    assert db.run_summary() == {"run_ts": None, "scanned": 0, "surfaced": 0, "verdicts": {}}
    db.close()


def test_migration_adds_surfaced_column_to_old_db(tmp_path):
    # Simulate a pre-migration DB: scored table without the `surfaced` column.
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """CREATE TABLE scored (
               id INTEGER PRIMARY KEY AUTOINCREMENT, run_ts REAL NOT NULL,
               lot_id TEXT NOT NULL, location TEXT, verdict TEXT, current_bid REAL,
               max_bid REAL, margin REAL, payload TEXT NOT NULL);"""
    )
    conn.execute(
        "INSERT INTO scored (run_ts, lot_id, verdict, payload) VALUES (1.0, '1', 'BID', '{}')"
    )
    conn.commit()
    conn.close()

    # Opening it via NellisDB should migrate it in place, defaulting old rows to 0.
    db = NellisDB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(scored)")}
    assert "surfaced" in cols
    assert db.conn.execute("SELECT surfaced FROM scored WHERE lot_id='1'").fetchone()["surfaced"] == 0
    # And new runs still write fine post-migration.
    db.record_run([_scored("2")], surfaced=[_scored("2")])
    assert db.run_summary()["surfaced"] == 1
    db.close()
