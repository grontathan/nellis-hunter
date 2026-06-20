"""SQLite persistence: dedup (`seen`) + full scored-run history (`scored`).

Two responsibilities:
  - `seen`  : one row per lot_id, the dedup memory. Lets the pipeline surface a
              lot once, then re-surface it only if it's closing soon and still
              under our max bid.
  - `scored`: append-only log of every scored lot per run, for later analysis.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .models import ScoredLot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    lot_id            TEXT PRIMARY KEY,
    first_seen_ts     REAL NOT NULL,
    last_seen_ts      REAL NOT NULL,
    last_current_bid  REAL,
    last_max_bid      REAL,
    last_verdict      TEXT,
    close_time_ts     REAL,
    surfaced_count    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scored (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts    REAL NOT NULL,
    lot_id    TEXT NOT NULL,
    location  TEXT,
    verdict   TEXT,
    current_bid REAL,
    max_bid   REAL,
    margin    REAL,
    surfaced  INTEGER NOT NULL DEFAULT 0,  -- 1 if this lot made the run's digest
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scored_run ON scored(run_ts);
CREATE INDEX IF NOT EXISTS idx_scored_lot ON scored(lot_id);
"""


def _close_ts(scored: ScoredLot) -> float | None:
    ct = scored.lot.close_time
    return ct.timestamp() if ct else None


class NellisDB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created before a column existed. SQLite has
        no IF NOT EXISTS for columns, so probe table_info and ALTER on demand."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(scored)")}
        if "surfaced" not in cols:
            self.conn.execute("ALTER TABLE scored ADD COLUMN surfaced INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "NellisDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- dedup --
    def get_seen(self, lot_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM seen WHERE lot_id = ?", (lot_id,)).fetchone()

    def should_surface(self, scored: ScoredLot, resurface_within_hours: float) -> bool:
        """New lots always surface. A previously-seen lot re-surfaces only if it's
        closing within the window AND its current bid is still under our max bid
        (i.e. the opportunity is live and time-sensitive)."""
        row = self.get_seen(scored.lot_id)
        if row is None:
            return True
        lot, sc = scored.lot, scored.scoring
        hrs = lot.hours_until_close
        if hrs is None or hrs < 0 or hrs > resurface_within_hours:
            return False
        if sc.max_bid is None or lot.current_bid is None:
            return False
        return lot.current_bid < sc.max_bid

    def mark_seen(self, scored: ScoredLot, *, surfaced: bool) -> None:
        now = time.time()
        lot, sc = scored.lot, scored.scoring
        existing = self.get_seen(lot.lot_id)
        bump = 1 if surfaced else 0
        if existing is None:
            self.conn.execute(
                """INSERT INTO seen (lot_id, first_seen_ts, last_seen_ts, last_current_bid,
                       last_max_bid, last_verdict, close_time_ts, surfaced_count)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    lot.lot_id, now, now, lot.current_bid, sc.max_bid,
                    sc.verdict.value, _close_ts(scored), bump,
                ),
            )
        else:
            self.conn.execute(
                """UPDATE seen SET last_seen_ts=?, last_current_bid=?, last_max_bid=?,
                       last_verdict=?, close_time_ts=?, surfaced_count=surfaced_count+?
                   WHERE lot_id=?""",
                (
                    now, lot.current_bid, sc.max_bid, sc.verdict.value,
                    _close_ts(scored), bump, lot.lot_id,
                ),
            )
        self.conn.commit()

    # -- run history --
    def record_run(
        self,
        scored_lots: list[ScoredLot],
        *,
        surfaced: list[ScoredLot] | None = None,
        run_ts: float | None = None,
    ) -> None:
        run_ts = run_ts or time.time()
        surfaced_ids = {s.lot_id for s in (surfaced or [])}
        rows = []
        for s in scored_lots:
            rows.append(
                (
                    run_ts, s.lot.lot_id, s.lot.location, s.scoring.verdict.value,
                    s.lot.current_bid, s.scoring.max_bid,
                    s.scoring.projected_margin_at_current_bid,
                    1 if s.lot_id in surfaced_ids else 0,
                    json.dumps(s.summary()),
                )
            )
        self.conn.executemany(
            """INSERT INTO scored (run_ts, lot_id, location, verdict, current_bid,
                   max_bid, margin, surfaced, payload) VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()

    def run_summary(self, run_ts: float | None = None) -> dict:
        """Counts for a run (latest by default): total scored, surfaced, and a
        verdict breakdown. The surfaced count is now an exact column read."""
        if run_ts is None:
            row = self.conn.execute("SELECT MAX(run_ts) AS ts FROM scored").fetchone()
            run_ts = row["ts"] if row else None
        if run_ts is None:
            return {"run_ts": None, "scanned": 0, "surfaced": 0, "verdicts": {}}
        scanned = self.conn.execute(
            "SELECT COUNT(*) AS n FROM scored WHERE run_ts=?", (run_ts,)
        ).fetchone()["n"]
        surfaced = self.conn.execute(
            "SELECT COUNT(*) AS n FROM scored WHERE run_ts=? AND surfaced=1", (run_ts,)
        ).fetchone()["n"]
        verdicts = {
            r["verdict"]: r["n"]
            for r in self.conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM scored WHERE run_ts=? GROUP BY verdict",
                (run_ts,),
            )
        }
        return {"run_ts": run_ts, "scanned": scanned, "surfaced": surfaced, "verdicts": verdicts}

    def last_run_ts(self) -> datetime | None:
        row = self.conn.execute("SELECT MAX(run_ts) AS ts FROM scored").fetchone()
        if row and row["ts"]:
            return datetime.fromtimestamp(row["ts"], tz=timezone.utc)
        return None
