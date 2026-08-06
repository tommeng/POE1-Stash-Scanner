"""poe.ninja's economy API — aggregated base-type prices.

`survey-bases` measures the same market directly from GGG's trade API, and the
two agree on ordering. What they do not agree on is sample size: a point-in-time
snapshot of *currently listed* white influenced bases is extremely thin. 39 ring
bases produced exactly one row clearing a five-listing confidence floor. This
endpoint returns 36 usable ring bases — and 853 across every slot — from a
single request.

It is **not independent evidence**. poe.ninja is built from the same public
stash data the trade API exposes, so this is better aggregation of one source
rather than a second opinion. `TradeClient.base_value` is kept as a spot-check
precisely so a third party can be verified against the source of truth.

Two things that cost real time to discover, worth keeping written down:

- The endpoint is **documented at poe.ninja/docs/api**. An earlier attempt
  concluded the API was dead because the old ``/api/data/*`` paths 404. They
  moved. The site is Astro with client-hydrated islands, so nothing about the
  request appears in the page HTML or the preloaded bundles — walking the module
  graph from the ``Poe1ItemOverviewPage`` island is what turns it up.
- **The league id is the ``id`` field from the leagues endpoint — ``Allflame``,
  capital A — not the URL slug ``allflame``.** The wrong one returns an empty
  ``lines`` array with HTTP 200 rather than an error, which reads exactly like
  "this league has no data". `resolve_league` exists to turn that silent
  nothing into a message.

Rows are per base × influence × item level, and thin ones are wild: a single
listed Helical Ring set its row to 30510 chaos. Filter on ``count`` before
believing anything, the same way the trade path does.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import USER_AGENT
from .config import DATA_DIR, ensure_dirs

BASE = "https://poe.ninja"
DOCS = f"{BASE}/docs/api"

# Prices move within a league, but not by the hour, and this is someone else's
# server. One fetch a day is plenty and keeps repeated commands free.
TTL_SECONDS = 24 * 3600


class NinjaError(RuntimeError):
    pass


@dataclass(frozen=True)
class BaseValue:
    """One base type, at one influence and item level."""

    name: str
    item_type: str          # "Ring", "Body Armour", ...
    influence: str | None   # "Shaper", "Shaper/Elder", None when uninfluenced
    ilvl: int
    chaos: float
    divine: float
    count: int              # listings behind the price; treat low as noise

    @property
    def confident(self) -> bool:
        from .trade import MIN_CONFIDENT_SAMPLE

        return self.count >= MIN_CONFIDENT_SAMPLE


def _cache_path(name: str) -> Path:
    return DATA_DIR / f"ninja-{name}.json"


def _get(url: str, params: dict | None = None) -> object:
    try:
        resp = httpx.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=60.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        raise NinjaError(f"Could not reach poe.ninja ({type(e).__name__}): {e}") from None
    if resp.status_code != 200:
        raise NinjaError(f"poe.ninja returned HTTP {resp.status_code} for {url}")
    try:
        return resp.json()
    except ValueError:
        raise NinjaError(f"poe.ninja returned non-JSON from {url}") from None


def leagues() -> list[str]:
    """League ids, in the exact form the overview endpoint requires."""
    data = _get(f"{BASE}/poe1/api/economy/leagues")
    return [str(e["id"]) for e in data if isinstance(e, dict) and e.get("id")]


def resolve_league(name: str) -> str:
    """Map a configured league name onto poe.ninja's id, case-insensitively.

    Passing the wrong form is not an error at the API - it answers 200 with an
    empty result - so getting this right here is what stops an empty table being
    mistaken for a league with no economy.
    """
    available = leagues()
    for candidate in available:
        if candidate.lower() == name.strip().lower():
            return candidate
    raise NinjaError(
        f"poe.ninja has no economy data for league {name!r}. "
        f"It knows: {', '.join(available)}"
    )


def fetch_base_types(league: str, force: bool = False) -> list[dict]:
    """Raw base-type rows for a league, cached to disk with a TTL."""
    ensure_dirs()
    path = _cache_path(f"basetypes-{league.lower().replace(' ', '-')}")
    if not force and path.exists() and (time.time() - path.stat().st_mtime) < TTL_SECONDS:
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass  # fall through and re-fetch

    data = _get(
        f"{BASE}/poe1/api/economy/stash/current/item/overview",
        params={"league": league, "type": "BaseType"},
    )
    lines = (data or {}).get("lines") if isinstance(data, dict) else None
    if not lines:
        raise NinjaError(
            f"poe.ninja returned no base-type rows for league {league!r}.\n"
            "The league id must be the `id` from the leagues endpoint (e.g. 'Allflame'), "
            "not the URL slug ('allflame') - the wrong one answers 200 with nothing."
        )
    path.write_text(json.dumps(lines))
    return lines


def base_values(
    league: str,
    item_type: str | None = None,
    influence: str | None = None,
    ilvl_min: int | None = None,
    min_samples: int = 0,
    force: bool = False,
) -> list[BaseValue]:
    """Normalised, filtered base values, most valuable first.

    ``influence`` matches poe.ninja's ``variant``: a single name like "Shaper",
    the literal "None" for uninfluenced, or None to keep every variant.
    """
    out: list[BaseValue] = []
    for row in fetch_base_types(league, force=force):
        variant = row.get("variant")
        variant = None if variant in (None, "None") else str(variant)
        value = BaseValue(
            name=str(row.get("name") or ""),
            item_type=str(row.get("itemType") or ""),
            influence=variant,
            ilvl=int(row.get("levelRequired") or 0),
            chaos=float(row.get("chaosValue") or 0.0),
            divine=float(row.get("divineValue") or 0.0),
            count=int(row.get("count") or 0),
        )
        if item_type and value.item_type.lower() != item_type.lower():
            continue
        if influence is not None:
            wanted = None if influence.lower() == "none" else influence
            if (value.influence or "").lower() != (wanted or "").lower():
                continue
        if ilvl_min and value.ilvl < ilvl_min:
            continue
        if value.count < min_samples:
            continue
        out.append(value)
    out.sort(key=lambda v: -v.chaos)
    return out


def item_types(league: str) -> list[str]:
    """Every slot present in the data, for `--slot`."""
    seen = {str(r.get("itemType") or "") for r in fetch_base_types(league)}
    return sorted(t for t in seen if t)
