"""SQLite cache of previous market checks.

Dump tabs are append-mostly: rescanning one re-presents mostly the same items.
Since an item's mods never change, a market check stays useful for days, and
caching it is what makes repeated scans affordable against the API budget.

The ``payload`` column is JSON and holds three keys:

    filters   the human-readable list of what the trade query compared on
    listings  the sampled listings, enough to recompute cheapest/median
    item      the item's own features - base type, ilvl, flags, mods

``item`` exists because every scan already pays for real price observations,
and pairing them with the item that produced them is the only cheap source of
evidence for calibrating the ruleset. Triage scores are currently hand-authored
priors; this is what would let them be measured instead. Written by
``scanner._features``; the cache itself stays dependency-free and treats it as
an opaque dict.

Note this is one row per item (upsert on ``item_id``), so a re-check replaces
the earlier observation rather than appending to it. That is the right
behaviour for a cache and adequate for calibration - each item is its own data
point - but it means no per-item price history.
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

    def put_check(self, item_id: str, league: str, result, features: dict | None = None) -> None:
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
                        "item": features or {},
                    }
                ),
            ),
        )
        self.conn.commit()

    def backfill_features(self, item_id: str, league: str, features: dict) -> bool:
        """Attach item features to a check that was stored without them.

        Rows written before feature logging existed still hold a perfectly good
        market observation; this fills in the item side so the accumulated
        cache becomes usable as calibration data rather than starting empty.

        Deliberately does *not* touch ``checked_at`` - refreshing that on every
        cache hit would make a row immortal and the item would never be
        re-priced.
        """
        row = self.conn.execute(
            "SELECT payload FROM market_check WHERE item_id = ? AND league = ?",
            (item_id, league),
        ).fetchone()
        if not row:
            return False
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if payload.get("item"):
            return False
        payload["item"] = features
        self.conn.execute(
            "UPDATE market_check SET payload = ? WHERE item_id = ? AND league = ?",
            (json.dumps(payload), item_id, league),
        )
        self.conn.commit()
        return True

    def observations(self, league: str | None = None) -> list[dict]:
        """Every check that carries item features, as (features + outcome).

        This is the calibration dataset: what the item was, paired with what
        the market said about it. Rows written before feature logging existed
        have no item side and are skipped rather than guessed at.
        """
        sql = "SELECT * FROM market_check"
        args: tuple = ()
        if league:
            sql += " WHERE league = ?"
            args = (league,)
        out: list[dict] = []
        for row in self.conn.execute(sql, args):
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                continue
            features = payload.get("item")
            if not features:
                continue
            out.append(
                {
                    **features,
                    "item_id": row["item_id"],
                    "league": row["league"],
                    "checked_at": row["checked_at"],
                    "total": row["total"] or 0,
                    "cheapest": row["cheapest"],
                    "median": row["median"],
                }
            )
        return out

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
