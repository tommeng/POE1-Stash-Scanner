"""SQLite cache of previous market checks.

Dump tabs are append-mostly: rescanning one re-presents mostly the same items.
Since an item's mods never change, a market check stays useful for days, and
caching it is what makes repeated scans affordable against the API budget.

The ``payload`` column is JSON and holds five keys:

    filters    the human-readable list of what the trade query compared on
    listings   the sampled listings, enough to recompute cheapest/median
    item       the item's own features - base type, ilvl, flags, mods
    relaxed    the strict query found nothing and was widened to one mod
    tightened  the strict query found thousands and was narrowed

``relaxed`` and ``tightened`` are caveats on the price, not details of how it
was fetched: a widened query priced the item's *strongest mod alone*, so its
median is not a like-for-like comparison and the report says so. Persisting
them is what stops a cached check from making a quieter, more confident claim
than the live one did - and with a 72h TTL most reads are cache hits.

Rows written before they were persisted have neither key, and unlike ``item``
there is nothing to backfill from: the flag was never recorded and the query
that would reveal it is gone. Such a row therefore reads as ``relaxed=False``,
which is the old bug living out the rest of its TTL. That is accepted rather
than fixed, because invalidating good price observations to re-earn a caveat
costs API budget. But note what the shape is - an absent value read as a
negative one, indistinguishable from a recorded negative. It ages out on its
own within 72 hours; nothing else in the payload gets to be read that way.

``item`` exists because every scan already pays for real price observations,
and pairing them with the item that produced them is the only cheap source of
evidence for calibrating the ruleset. Triage scores are currently hand-authored
priors; this is what would let them be measured instead. Written by
``scanner._features``; the cache itself stays dependency-free and treats it as
an opaque dict.

It carries a ``ruleset`` fingerprint, and ``label_features`` rewrites the whole
``item`` blob when that fingerprint goes stale. The rule ids in ``rules_hit``
are only meaningful next to the ruleset that defined them, and rules do get
retuned and split - at which point every earlier observation silently describes
rules that no longer exist. Re-labelling on a cache hit is free and keeps the
price observation while bringing its explanation up to date.

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
                        # Caveats on the price itself, so they have to survive
                        # the round trip or the cached report overstates.
                        "relaxed": bool(result.relaxed),
                        "tightened": bool(result.tightened),
                    }
                ),
            ),
        )
        self.conn.commit()

    def label_features(self, item_id: str, league: str, features: dict) -> bool:
        """Attach current item features to a stored check. Returns True if written.

        Writes in two cases, both on a cache hit that costs no API call:

        *Absent* - rows written before feature logging existed still hold a
        perfectly good market observation, so filling in the item side makes the
        accumulated cache usable as calibration data rather than starting empty.

        *Stale* - the stored label was written under a different ruleset, so its
        ``rules_hit`` describes rules that may no longer exist. The item itself
        has not changed, and the caller's features were just derived from it by
        the current ruleset, so re-labelling is a straight upgrade: the price
        observation is kept and its explanation is brought up to date. Without
        this, editing a rule permanently orphans every observation collected
        before the edit.

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
        stored = payload.get("item") or {}
        if stored and stored.get("ruleset") == features.get("ruleset"):
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
