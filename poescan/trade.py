"""Layer 2: market verification against the official trade API.

The trade search endpoint is public - no auth needed - but tightly limited
(roughly 30 searches per 5 minutes, 600 per 6 hours). Every call here is
budgeted, so only items that survived triage get one.

What we actually want out of this is not a precise valuation. It is the pair
(how many similar items are listed, what do the cheapest ones ask). A rare
with 400 competitors at 1c is junk; one with 6 competitors starting at 40c is
worth your attention.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

import httpx

from . import USER_AGENT
from .config import TRADE_BASE
from .items import Item
from .ratelimit import RateLimiter
from .triage import Verdict, notable_mods

SEARCH_POLICY = "trade-search-request-limit"
FETCH_POLICY = "trade-fetch-request-limit"
EXCHANGE_POLICY = "trade-exchange-request-limit"

# Only these convert cleanly to a chaos-denominated number. Anything else is
# reported in its own currency rather than guessing a rate.
BASE_RATES = {"chaos": 1.0}


@dataclass
class Listing:
    price_amount: float
    price_currency: str
    chaos_value: float | None
    account: str
    item_name: str
    indexed: str = ""
    mods: list[str] = field(default_factory=list)


@dataclass
class MarketResult:
    total: int = 0
    listings: list[Listing] = field(default_factory=list)
    query_url: str = ""
    error: str = ""
    filters_used: list[str] = field(default_factory=list)
    # True when the first, stricter query found nothing and we widened it.
    relaxed: bool = False
    # True when the first query matched so many items that we narrowed it.
    tightened: bool = False

    @property
    def confident(self) -> bool:
        """Enough priced comparables for the median to mean anything."""
        return len(self.priced) >= MIN_CONFIDENT_SAMPLE

    @property
    def priced(self) -> list[float]:
        return [l.chaos_value for l in self.listings if l.chaos_value is not None]

    @property
    def cheapest(self) -> float | None:
        vals = self.priced
        return min(vals) if vals else None

    @property
    def median(self) -> float | None:
        vals = self.priced
        return statistics.median(vals) if vals else None

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if self.total == 0:
            return "no comparable listings"
        med = self.median
        if med is None:
            return f"{self.total} listings, prices unparsed"
        base = f"{self.total} listings, cheapest {self.cheapest:.0f}c, median {med:.0f}c"
        if self.total > BROAD_RESULT_THRESHOLD:
            # Be explicit that this is a floor, not a valuation for this item.
            return base + " (common - that price is the market floor)"
        if not self.confident:
            return base + f" (only {len(self.priced)} priced - low confidence)"
        return base


# `status` decides which half of the market you are pricing against, and the
# two halves disagree wildly. Measured on one +1-spell-gem wand: "securable"
# (instant buyout) returned 4126 listings with a 90c median, while "online"
# (in person) returned 54 with a 4c median - the cheap in-person listings are
# largely bait that never sells. Instant buyout is both the truer price and
# the far larger, more stable sample, so it is the default.
STATUS_OPTIONS = {
    "securable": "Instant Buyout",
    "available": "Instant Buyout and In Person",
    "onlineleague": "In Person (Online in League)",
    "online": "In Person (Online)",
    "any": "Any",
}
DEFAULT_STATUS = "securable"

# Above this many matches, the cheapest listings describe the market floor
# rather than this item, so the search is re-run with more of the item's mods.
BROAD_RESULT_THRESHOLD = 300

# The trade API stops counting here. A total at the cap is a lower bound, not a
# total, which makes it useless as the denominator of any share or quantile.
_API_RESULT_CAP = 10000
MAX_TIGHT_STATS = 5

# Below this many priced listings, a median is noise rather than a market.
# Three unsold listings at 4 divine say "these did not sell", not "yours is
# worth 4 divine" - so results this thin are reported as low confidence and
# their influence on ranking is damped.
MIN_CONFIDENT_SAMPLE = 5

# Mods nobody buys an item *for*. Including them when narrowing a search finds
# coincidental matches rather than comparable items, which is how a common
# amulet ends up "priced" against three unsold outliers.
LOW_SIGNAL_MOD = re.compile(
    r"Accuracy Rating|Light Radius|Mana Regeneration|Stun (and Block )?Recovery|"
    r"Stun Threshold|Attribute Requirements|Flask Charges|"
    r"increased Rarity of Items|to Strength and Intelligence|"
    r"to Strength and Dexterity|to Dexterity and Intelligence",
    re.IGNORECASE,
)


class TradeClient:
    def __init__(
        self,
        league: str,
        limiter: RateLimiter | None = None,
        timeout: float = 30.0,
        status: str = DEFAULT_STATUS,
    ) -> None:
        self.league = league
        self.status = status if status in STATUS_OPTIONS else DEFAULT_STATUS
        self.limiter = limiter or RateLimiter()
        self.rates = dict(BASE_RATES)
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, url: str, policy: str, **kw) -> httpx.Response:
        self.limiter.wait(policy)
        resp = self._client.request(method, url, **kw)
        self.limiter.observe(resp)
        if resp.status_code == 429:
            # Respect the ban, then make exactly one more attempt.
            self.limiter.wait(policy)
            resp = self._client.request(method, url, **kw)
            self.limiter.observe(resp)
        return resp

    def budget(self) -> tuple[int, float] | None:
        return self.limiter.bucket(SEARCH_POLICY).budget_remaining()

    # -- currency ---------------------------------------------------------

    def load_rates(self) -> None:
        """Derive a divine->chaos rate from the bulk exchange, once per scan."""
        body = {
            "query": {"status": {"option": "online"}, "have": ["chaos"], "want": ["divine"]},  # bulk exchange has no securable concept
            "sort": {"have": "asc"},
            "engine": "new",
        }
        try:
            resp = self._request(
                "POST", f"{TRADE_BASE}/api/trade/exchange/{self.league}", EXCHANGE_POLICY, json=body
            )
            if resp.status_code != 200:
                return
            ratios: list[float] = []
            for entry in (resp.json().get("result") or {}).values():
                for offer in (entry.get("listing") or {}).get("offers") or []:
                    have = float((offer.get("exchange") or {}).get("amount") or 0)
                    want = float((offer.get("item") or {}).get("amount") or 0)
                    if have > 0 and want > 0:
                        ratios.append(have / want)
            # The cheapest offers are usually scams (1 chaos for 1 divine),
            # so take the median rather than the minimum.
            if ratios:
                self.rates["divine"] = statistics.median(ratios)
        except (httpx.HTTPError, ValueError, KeyError):
            return

    def to_chaos(self, amount: float, currency: str) -> float | None:
        rate = self.rates.get(currency)
        return amount * rate if rate else None

    # -- query building ---------------------------------------------------

    def build_query(
        self, item: Item, verdict: Verdict, slack: float = 0.9, max_stats: int = 4
    ) -> "tuple[dict, list[str]]":
        """Search for items comparable to this one.

        Returns the request body and a human-readable list of what it compared
        on, for the report.

        Filters are the item's own rolls, relaxed by ``slack`` so that near
        misses still count as comparable. Capped at ``max_stats``: an
        over-specified query returns zero results and tells us nothing. A good
        item will genuinely have no exact peers listed, so ``price_check``
        widens the query rather than concluding the item is unique.
        """
        type_filters: dict = {"rarity": {"option": "rare"}}
        if item.category and item.category != "unknown":
            type_filters["category"] = {"option": item.category}
        if item.ilvl >= 84:
            type_filters["ilvl"] = {"min": 84}

        # Only compare against listings with a committed buyout price. Items
        # listed without one have to be negotiated in whisper, so their asking
        # price is unknown - counting them would inflate "how many are listed"
        # without contributing anything to the median. This is currently also
        # the API's default, but pinning it means a change to that default
        # cannot silently corrupt the price signal.
        filters: dict = {
            "type_filters": {"filters": type_filters},
            "trade_filters": {"filters": {"sale_type": {"option": "priced"}}},
        }
        if item.influences:
            filters["misc_filters"] = {
                "filters": {f"{inf}_item": {"option": "true"} for inf in item.influences}
            }

        stat_filters: list[dict] = []
        described: list[str] = []

        for mod in notable_mods(verdict, limit=max(1, max_stats - 1)):
            if not mod.stat_id or mod.stat_id.startswith("pseudo."):
                continue
            entry: dict = {"id": mod.stat_id, "disabled": False}
            # Mods that roll negative (mana cost reduction) are better the more
            # negative they are, so they need an upper bound. Scaling by slack
            # moves it toward zero, which widens the search - dividing would
            # narrow it.
            if mod.value < 0:
                entry["value"] = {"max": round(mod.value * slack, 2)}
            elif mod.value:
                entry["value"] = {"min": round(mod.value * slack, 2)}
            stat_filters.append(entry)
            described.append(mod.text)

        # Pseudo totals matter most on gear whose value is defensive.
        pseudo = verdict.pseudo.as_filters()
        for key in ("pseudo.pseudo_total_life", "pseudo.pseudo_total_elemental_resistance"):
            if len(stat_filters) >= max_stats:
                break
            if key in pseudo and pseudo[key] >= 40:
                stat_filters.append({"id": key, "value": {"min": round(pseudo[key] * slack)}, "disabled": False})
                described.append(f"{key.split('_total_')[-1].replace('_', ' ')} >= {round(pseudo[key] * slack)}")

        # Only the mods that earned score are "notable", but an item with two
        # notable mods and four ordinary ones still needs those four to find
        # genuine peers. Fill any remaining slots with the biggest rolls left,
        # strongest first - used when tightening an over-broad search.
        if len(stat_filters) < max_stats:
            used = {f["id"] for f in stat_filters}
            rest = [
                m for m in item.mods_in("explicit", "implicit", "crafted", "fractured")
                if m.stat_id
                and m.stat_id not in used
                and not m.stat_id.startswith("pseudo.")
                and not LOW_SIGNAL_MOD.search(m.text)
            ]
            # Magnitude is not importance - a big Accuracy roll is still noise -
            # so this only ranks among mods already judged price-relevant.
            for mod in sorted(rest, key=lambda m: -abs(m.value)):
                if len(stat_filters) >= max_stats:
                    break
                used.add(mod.stat_id)
                entry = {"id": mod.stat_id, "disabled": False}
                if mod.value < 0:
                    entry["value"] = {"max": round(mod.value * slack, 2)}
                elif mod.value:
                    entry["value"] = {"min": round(mod.value * slack, 2)}
                stat_filters.append(entry)
                described.append(mod.text)

        query: dict = {
            "query": {"status": {"option": self.status}, "filters": filters},
            "sort": {"price": "asc"},
        }
        if stat_filters:
            query["query"]["stats"] = [{"type": "and", "filters": stat_filters}]
        return query, described

    # -- the actual check -------------------------------------------------

    def _search(self, query: dict) -> "tuple[dict | None, str]":
        try:
            resp = self._request(
                "POST", f"{TRADE_BASE}/api/trade/search/{self.league}", SEARCH_POLICY, json=query
            )
        except httpx.HTTPError as e:
            return None, f"search failed: {e}"
        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")
            except ValueError:
                pass
            return None, f"search returned HTTP {resp.status_code}{': ' + detail if detail else ''}"
        return resp.json(), ""

    def price_check(self, item: Item, verdict: Verdict, sample: int = 10) -> MarketResult:
        query, described = self.build_query(item, verdict)
        result = MarketResult(filters_used=described)

        data, err = self._search(query)
        if data is None:
            result.error = err
            return result

        n_filters = len(query["query"].get("stats", [{}])[0].get("filters", []))

        # Two failure modes, opposite directions, both uninformative.
        #
        # Nothing matched: happens most for exactly the items worth pricing, so
        # widen to the single strongest mod and learn what that alone fetches.
        if _empty(data) and n_filters > 1:
            wide_query, wide_described = self.build_query(item, verdict, slack=0.75, max_stats=1)
            wide_data, _err = self._search(wide_query)
            if wide_data is not None and not _empty(wide_data):
                data = wide_data
                result.filters_used = wide_described
                result.relaxed = True

        # Thousands matched: the cheapest listings are then the market floor
        # rather than anything comparable, and "median 1c" says nothing about
        # this item. Add the item's remaining mods to find real peers.
        elif int(data.get("total") or 0) > BROAD_RESULT_THRESHOLD and n_filters < MAX_TIGHT_STATS:
            tight_query, tight_described = self.build_query(
                item, verdict, slack=0.95, max_stats=MAX_TIGHT_STATS
            )
            if len(tight_query["query"].get("stats", [{}])[0].get("filters", [])) > n_filters:
                tight_data, _err = self._search(tight_query)
                # Keep the broad answer if narrowing overshot to nothing.
                if tight_data is not None and not _empty(tight_data):
                    data = tight_data
                    result.filters_used = tight_described
                    result.tightened = True

        query_id = data.get("id", "")
        ids = data.get("result") or []
        result.total = int(data.get("total") or len(ids))
        result.query_url = f"{TRADE_BASE}/trade/search/{self.league}/{query_id}" if query_id else ""

        if not ids:
            return result

        return self._fetch_listings(result, query_id, ids, sample)

    def _fetch_listings(
        self, result: MarketResult, query_id: str, ids: list, sample: int
    ) -> MarketResult:
        """Read prices for the cheapest matches and attach them to ``result``."""
        want = ids[:sample]
        try:
            fresp = self._request(
                "GET",
                f"{TRADE_BASE}/api/trade/fetch/{','.join(want)}",
                FETCH_POLICY,
                params={"query": query_id},
            )
        except httpx.HTTPError as e:
            result.error = f"fetch failed: {e}"
            return result

        if fresp.status_code != 200:
            result.error = f"fetch returned HTTP {fresp.status_code}"
            return result

        for row in fresp.json().get("result") or []:
            if not row:
                continue
            listing = row.get("listing") or {}
            price = listing.get("price") or {}
            amount = float(price.get("amount") or 0)
            currency = str(price.get("currency") or "")
            it = row.get("item") or {}
            result.listings.append(
                Listing(
                    price_amount=amount,
                    price_currency=currency,
                    chaos_value=self.to_chaos(amount, currency) if amount else None,
                    account=((listing.get("account") or {}).get("name") or ""),
                    item_name=f"{it.get('name', '')} {it.get('typeLine', '')}".strip(),
                    indexed=listing.get("indexed", ""),
                    mods=[_mod_desc(m) for m in (it.get("explicitMods") or [])],
                )
            )
        return result


    # -- base type survey -------------------------------------------------

    def base_market(
        self,
        base_type: str,
        ilvl_min: int | None = None,
        price_min: float | None = None,
        influence: str | None = None,
        rarity: str = "rare",
        sample: int = 0,
    ) -> MarketResult:
        """How much of one base type's market sits above a price threshold.

        Carries *no* stat filters, so the only things varying are the base, the
        item level, and the price floor. Run identically across every base in a
        slot, the share of listings above the threshold is a usable relative
        measure of whether buyers pay extra for the base itself.

        Measuring the cheap end instead does not work, and the reason is worth
        keeping: sorting ascending across a saturated market finds the market
        floor, which is 1 chaos for every base that has one. A survey built on
        the cheapest listings returns 1c for good and bad bases alike. Worse,
        floor listings are mostly priced in alchemy and alteration orbs, which
        ``to_chaos`` cannot convert, so they drop out of ``priced`` entirely.

        ``sample`` defaults to 0 - the count is the measurement, so no fetch is
        needed and a survey costs one search per base.
        """
        trade_filters: dict = {"sale_type": {"option": "priced"}}
        if price_min:
            trade_filters["price"] = {"min": price_min}
        query_body: dict = {
            "status": {"option": self.status},
            # The trade site puts base type at the top of the query, beside
            # status, rather than among the type_filters. Both forms are
            # accepted; this is the canonical one.
            "type": base_type,
            "filters": {
                "type_filters": {"filters": {"rarity": {"option": rarity}}},
                "trade_filters": {"filters": trade_filters},
            },
        }
        described = [f"base = {base_type}", rarity]
        if ilvl_min:
            query_body["filters"]["type_filters"]["filters"]["ilvl"] = {"min": ilvl_min}
            described.append(f"ilvl >= {ilvl_min}")
        if influence:
            query_body["filters"]["misc_filters"] = {
                "filters": {f"{influence}_item": {"option": "true"}}
            }
            described.append(f"{influence} influenced")
        if price_min:
            described.append(f"price >= {price_min:g}c")

        result = MarketResult(filters_used=described)
        data, err = self._search({"query": query_body, "sort": {"price": "asc"}})
        if data is None:
            result.error = err
            return result

        query_id = data.get("id", "")
        ids = data.get("result") or []
        result.total = int(data.get("total") or len(ids))
        result.query_url = (
            f"{TRADE_BASE}/trade/search/{self.league}/{query_id}" if query_id else ""
        )
        if not ids or sample <= 0:
            return result
        return self._fetch_listings(result, query_id, ids, sample)


    def base_value(
        self,
        base_type: str,
        ilvl_min: int | None = None,
        influence: str | None = None,
        sample: int = 8,
    ) -> MarketResult:
        """What an unrolled base of this type sells for.

        Queries **white** (normal rarity) items, not rares. That is the whole
        trick, and it took four failed designs to find it: a rare item's price
        is dominated by its mods, so the base contributes a sliver of the
        variance and gets swamped. Measured live, every mod-carrying design
        ranked Sapphire and Iron Rings above Opal - the exact inverse of the
        truth. A white base has no mods, so its price *is* the base's price.

        The designs that failed, so nobody re-tries them:

        - *Floor price of rares* returns 1 chaos for every base with a market,
          because sorting ascending across a saturated market finds the market
          floor rather than anything about the base.
        - *Share of rares above a price* divides by a total that saturates at
          the API's 10000 cap, inflating exactly the popular bases.
        - *A fixed target count* is not a fixed quantile: top-300 of a base
          with 50000 listings is its 99th percentile, top-300 of one with 1700
          is its 83rd.
        - *A fixed fraction of rares* removes that error and still inverts,
          because the mods were always the confound, not the normalisation.

        ``influence`` matters more than it looks: the market for crafting bases
        is overwhelmingly influenced ones, so an uninfluenced query finds
        almost nothing. A total of zero is itself a strong signal - nobody
        bothers to sell that base unrolled.

        One search plus one fetch per base.
        """
        result = self.base_market(
            base_type,
            ilvl_min=ilvl_min,
            influence=influence,
            rarity="normal",
            sample=sample,
        )
        return result


def _empty(data: dict) -> bool:
    """A search that matched nothing, by either the id list or the count."""
    return not (data.get("result") or []) and not int(data.get("total") or 0)


def _mod_desc(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("description", ""))
    return str(entry)
