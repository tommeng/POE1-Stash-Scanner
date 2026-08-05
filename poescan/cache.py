"""SQLite cache of previous market checks.

Dump tabs are append-mostly: rescanning one re-presents mostly the same items.
Since an item's mods never change, a market check stays useful for days, and
caching it is what makes repeated scans affordable against the API budget.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from .config import CACHE_DB, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_check (
    item_id     TEXT PRIMARY KEY,
    league      TEXT NOT NULL,
    checked_at  REAL NOT NULL,
    total       INTEGER,
    cheapest    REAL,
    median      REAL,
    query_url   TEXT,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS market_check_league ON market_check(league, checked_at);

CREATE TABLE IF NOT EXISTS dismissed (
    item_id     TEXT PRIMARY KEY,
    dismissed_at REAL NOT NULL,
    note        TEXT
);
"""


@dataclass
class CachedCheck:
    total: int
    cheapest: float | None
    median: float | None
    query_url: str
    checked_at: float
    payload: dict

    def age_hours(self) -> float:
        return (time.time() - self.checked_at) / 3600.0


class Cache:
    def __init__(self, path=None) -> None:
        ensure_dirs()
        self.conn = sqlite3.connect(str(path or CACHE_DB))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get_check(self, item_id: str, league: str, max_age_hours: float = 72.0) -> CachedCheck | None:
        row = self.conn.execute(
            "SELECT * FROM market_check WHERE item_id = ? AND league = ?", (item_id, league)
        ).fetchone()
        if not row:
            return None
        if (time.time() - row["checked_at"]) > max_age_hours * 3600:
            return None
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return CachedCheck(
            total=row["total"] or 0,
            cheapest=row["cheapest"],
            median=row["median"],
            query_url=row["query_url"] or "",
            checked_at=row["checked_at"],
            payload=payload,
        )

    def put_check(self, item_id: str, league: str, result) -> None:
        self.conn.execute(
            """INSERT INTO market_check
                 (item_id, league, checked_at, total, cheapest, median, query_url, payload)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET
                 league=excluded.league, checked_at=excluded.checked_at,
                 total=excluded.total, cheapest=excluded.cheapest,
                 median=excluded.median, query_url=excluded.query_url,
                 payload=excluded.payload""",
            (
                item_id,
                league,
                time.time(),
                result.total,
                result.cheapest,
                result.median,
                result.query_url,
                json.dumps(
                    {
                        "filters": result.filters_used,
                        "listings": [
                            {
                                "amount": l.price_amount,
                                "currency": l.price_currency,
                                "chaos": l.chaos_value,
                                "name": l.item_name,
                            }
                            for l in result.listings
                        ],
                    }
                ),
            ),
        )
        self.conn.commit()

    def dismiss(self, item_id: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO dismissed (item_id, dismissed_at, note) VALUES (?,?,?)",
            (item_id, time.time(), note),
        )
        self.conn.commit()

    def dismissed_ids(self) -> set[str]:
        return {r["item_id"] for r in self.conn.execute("SELECT item_id FROM dismissed")}

    def stats(self) -> dict:
        checks = self.conn.execute("SELECT COUNT(*) c FROM market_check").fetchone()["c"]
        dismissed = self.conn.execute("SELECT COUNT(*) c FROM dismissed").fetchone()["c"]
        return {"market_checks": checks, "dismissed": dismissed}
