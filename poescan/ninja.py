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
sampled Helical Ring set its row to 30510 chaos. Filter on ``samples`` before
believing anything, the same way the trade path does.

Three properties of the payload that are not obvious and will mislead you:

- **``count`` is a capped sample size, not a listing count.** It saturates at
  exactly 399 — 1188 of the 19448 rows sit on that ceiling — while the same rows
  carry ``listingCount`` up to 76869. It is the right field for a confidence
  floor and the wrong one for anything resembling scarcity, since a saturating
  denominator is precisely the mistake CLAUDE.md records for the "share of rares
  above a price" survey design. Exposed here as ``samples`` and ``listings``
  respectively, so the distinction survives being read quickly.
- **Uninfluenced rows omit ``variant`` entirely.** The literal string ``"None"``
  appears zero times in 19448 rows, despite being the obvious guess.
- **``levelRequired`` only ever takes the values 82-86**, and it is the item
  level tier rather than a wielding requirement. This dataset therefore prices
  *crafting bases* and nothing else — an ilvl 74 rare out of a dump tab has no
  row here at any influence.

Every one of the 918 base names appears in GGG's own item data
(``tradedata.base_type_names``), so ``name`` is a clean join key. ``item_type``
is not: it is poe.ninja's own vocabulary, folding sceptres into "One Handed
Mace" and rune daggers into "Dagger", which the repo keeps distinct.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import USER_AGENT
from .config import DATA_DIR, ensure_dirs
from .trade import MIN_CONFIDENT_SAMPLE

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
    item_type: str          # poe.ninja's vocabulary, not the repo's categories
    influence: str | None   # "Shaper", "Shaper/Elder", None when uninfluenced
    ilvl: int
    chaos: float
    divine: float
    samples: int            # poe.ninja's `count` - CAPPED AT 399, see module docstring
    listings: int           # poe.ninja's `listingCount` - the actual market size

    @property
    def influences(self) -> tuple[str, ...]:
        """The individual influences, so "Shaper/Elder" answers to "Shaper"."""
        return tuple(p for p in (self.influence or "").split("/") if p)

    @property
    def confident(self) -> bool:
        """Enough sampled items for the price to mean anything.

        Uses `samples` rather than `listings` on purpose: the price was computed
        from the sample, so that is what its reliability depends on.
        """
        return self.samples >= MIN_CONFIDENT_SAMPLE


def _cache_path(name: str) -> Path:
    # Library callers pass an arbitrary league string; keep it out of the path.
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.lower())
    return DATA_DIR / f"ninja-{safe}.json"


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
    if not isinstance(data, list):
        raise NinjaError("poe.ninja's leagues endpoint returned an unexpected shape")
    found = [str(e["id"]) for e in data if isinstance(e, dict) and e.get("id")]
    if not found:
        raise NinjaError("poe.ninja's leagues endpoint listed no leagues")
    return found


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
            cached = json.loads(path.read_text())
            # Valid JSON of the wrong shape would otherwise fail much later,
            # inside row.get(), as an AttributeError rather than a NinjaError.
            if isinstance(cached, list):
                return cached
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
    # Atomic, so an interrupt cannot leave a truncated 9.7MB cache behind.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(lines))
    tmp.replace(path)
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
    rows = fetch_base_types(league, force=force)
    # Reject an unknown filter rather than returning nothing. An empty table is
    # indistinguishable from a genuine empty result, which is the same trap
    # `resolve_league` exists to close on the league parameter.
    if item_type:
        item_type = _one_of(item_type, _item_types(rows), "item type")
    if influence is not None and influence.lower() != "none":
        influence = _one_of(influence, _influences(rows), "influence")

    out: list[BaseValue] = []
    for row in rows:
        # Uninfluenced rows omit `variant`; the literal "None" never appears,
        # but tolerate it in case that ever changes.
        variant = row.get("variant")
        variant = None if variant in (None, "None") else str(variant)
        value = BaseValue(
            name=str(row.get("name") or ""),
            item_type=str(row.get("itemType") or ""),
            influence=variant,
            ilvl=int(row.get("levelRequired") or 0),
            chaos=float(row.get("chaosValue") or 0.0),
            divine=float(row.get("divineValue") or 0.0),
            samples=int(row.get("count") or 0),
            listings=int(row.get("listingCount") or 0),
        )
        if item_type and value.item_type.lower() != item_type.lower():
            continue
        if influence is not None:
            if influence.lower() == "none":
                if value.influence is not None:
                    continue
            # A Shaper/Elder base is still a Shaper base, so match on the
            # components rather than the whole string - otherwise asking for
            # Shaper silently drops 492 compound rows.
            elif influence.lower() not in {i.lower() for i in value.influences}:
                continue
        if ilvl_min and value.ilvl < ilvl_min:
            continue
        if value.samples < min_samples:
            continue
        out.append(value)
    out.sort(key=lambda v: -v.chaos)
    return out


def _one_of(wanted: str, known: list[str], what: str) -> str:
    for candidate in known:
        if candidate.lower() == wanted.strip().lower():
            return candidate
    raise NinjaError(f"Unknown {what} {wanted!r}. Available: {', '.join(known)}")


def _item_types(rows: list[dict]) -> list[str]:
    return sorted({str(r.get("itemType") or "") for r in rows} - {""})


def _influences(rows: list[dict]) -> list[str]:
    """Every individual influence, compounds split into their parts."""
    out: set[str] = set()
    for r in rows:
        out.update(p for p in str(r.get("variant") or "").split("/") if p)
    return sorted(out)


def item_types(league: str) -> list[str]:
    """Every slot present in the data, for `--slot`."""
    return _item_types(fetch_base_types(league))


def influences(league: str) -> list[str]:
    """Every individual influence present, for `--influence`."""
    return _influences(fetch_base_types(league))
