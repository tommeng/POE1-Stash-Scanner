"""Ties the stages together: read stash -> triage -> verify -> results."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .cache import Cache
from .config import RATELIMIT_STATE, Config
from .items import Item
from .ratelimit import RateLimiter
from .stash import LegacyStashClient, StashClient, Tab
from .trade import BROAD_RESULT_THRESHOLD as BROAD_MARKET
from .trade import DEFAULT_STATUS, MarketResult, TradeClient
from .tradedata import StatIndex
from .triage import Ruleset, Verdict, triage


@dataclass
class Candidate:
    verdict: Verdict
    market: MarketResult | None = None
    from_cache: bool = False

    @property
    def item(self) -> Item:
        return self.verdict.item

    @property
    def interest(self) -> float:
        """Ranking score, dominated by the market rather than by the rules.

        Measured on a real dump tab, triage score had no useful correlation
        with price: the highest-scoring item (31) was worth 2c, while items
        scoring 12 ranged from 1c to 25c. What *did* track value was scarcity -
        5-21 comparable listings meant 20-65c, while 500+ meant 1-2c.

        So a checked item is ranked on what the market said; the triage score
        only breaks ties. Unchecked items fall back to their triage score,
        which lands them mid-pack - ahead of confirmed junk, behind confirmed
        value, which is the honest place for "we do not know yet".
        """
        m = self.market
        if not m or m.error:
            return self.verdict.score

        if m.total == 0:
            # Nothing comparable listed: either genuinely scarce or the query
            # was over-specified. Worth a look, but it is not evidence of price.
            return 45.0 + min(self.verdict.score / 4.0, 10.0)

        median = m.median
        if median is None:
            return self.verdict.score

        if median >= 300:
            market = 100.0
        elif median >= 80:
            market = 80.0
        elif median >= 25:
            market = 60.0
        elif median >= 10:
            market = 40.0
        elif m.total > BROAD_MARKET:
            # Cheap and abundant - the clearest junk signal available.
            market = 0.0
        else:
            market = 18.0

        # A high price drawn from a handful of unsold listings is not evidence.
        if market > 40 and not m.confident:
            market = 40.0 + (market - 40.0) * 0.4

        # Scarcity is the second-strongest signal after price.
        if m.total <= 25 and m.confident:
            market += 8.0

        return market + min(self.verdict.score / 4.0, 10.0)


@dataclass
class ScanReport:
    league: str
    started_at: float = field(default_factory=time.time)
    tabs_scanned: list[str] = field(default_factory=list)
    total_items: int = 0
    rare_items: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    market_checks: int = 0
    cache_hits: int = 0
    notes: list[str] = field(default_factory=list)
    scanned_label: str = ""

    @property
    def duration(self) -> float:
        return time.time() - self.started_at


def open_stash(cfg: Config, token, limiter: RateLimiter):
    """Whichever stash client this install is configured for."""
    if cfg.auth_mode == "poesessid":
        return LegacyStashClient(cfg.poesessid, cfg.account_name, cfg.league, limiter)
    return StashClient(token, cfg.league, limiter)


# Special tabs are physically incapable of holding rare gear, so scanning them
# is wasted requests. Excluded by default; name one explicitly to override.
NON_GEAR_TAB_TYPES = {
    "currencystash", "mapstash", "fragmentstash", "gemstash", "flaskstash",
    "deliriumstash", "blightstash", "ultimatumstash", "delvestash",
    "uniquestash", "essencestash", "divinationcardstash", "metamorphstash",
    "coffinstash",
}


def is_gear_tab(tab: Tab) -> bool:
    return tab.type.lower() not in NON_GEAR_TAB_TYPES


def select_tabs(tabs: list[Tab], wanted: list[str]) -> list[Tab]:
    """Resolve tab selectors to tabs.

    Tab names are frequently bare numbers and are not unique, so matching has
    to be precise rather than helpful:

      ``Misc``   exact name, case-insensitive (may match several tabs)
      ``#20``    tab index - the only way to disambiguate duplicate names
      ``dump``   substring, used *only* when nothing matched exactly

    An empty selection means every tab that can hold gear.
    """
    if not wanted:
        return [t for t in tabs if is_gear_tab(t)]

    out: list[Tab] = []
    seen: set[int] = set()

    def take(t: Tab) -> None:
        if id(t) not in seen:
            seen.add(id(t))
            out.append(t)

    for w in wanted:
        needle = str(w).strip()
        if not needle:
            continue

        if needle.startswith("#"):
            key = needle[1:].strip()
            for t in tabs:
                if str(t.index) == key:
                    take(t)
            continue

        low = needle.lower()
        exact = [t for t in tabs if t.name.lower() == low]
        if exact:
            for t in exact:
                take(t)
            continue

        # Only fall back to fuzzy matching when there is no exact name.
        for t in tabs:
            if low in t.name.lower():
                take(t)

    return out


def scan(
    cfg: Config,
    token,
    index: StatIndex,
    ruleset: Ruleset,
    tabs_wanted: list[str] | None = None,
    verify: bool = True,
    max_checks: int | None = None,
    cache_hours: float = 72.0,
    status: str = DEFAULT_STATUS,
    progress=None,
) -> ScanReport:
    limiter = RateLimiter(state_path=RATELIMIT_STATE)
    report = ScanReport(league=cfg.league)
    tabs_wanted = tabs_wanted if tabs_wanted is not None else list(cfg.tabs or [])

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    with open_stash(cfg, token, limiter) as stash:
        say("Listing stash tabs...")
        all_tabs = stash.flat_tabs()
        chosen = select_tabs(all_tabs, tabs_wanted)
        if not chosen:
            report.notes.append(
                f"No tabs matched {tabs_wanted!r}. Available: "
                + ", ".join(t.name for t in all_tabs[:20])
            )
            return report

        report.scanned_label = ", ".join(f"{t.name} (#{t.index})" for t in chosen)
        items: list[Item] = []
        for tab in chosen:
            say(f"Reading tab '{tab.name}' (#{tab.index})...")
            tab_items = stash.tab_items(tab, index)
            items.extend(tab_items)
            report.tabs_scanned.append(tab.name)

    report.total_items = len(items)
    rares = [i for i in items if i.rarity == "rare"]
    report.rare_items = len(rares)
    say(f"{len(items)} items, {len(rares)} rare. Scoring...")

    verdicts = triage(items, ruleset)
    promoted = [v for v in verdicts if v.promoted]
    say(f"{len(promoted)} passed triage.")

    cap = max_checks if max_checks is not None else ruleset.max_market_checks
    candidates = [Candidate(verdict=v) for v in promoted]

    # Recorded usage must reach disk even if the scan dies part-way.
    # observe() self-saves but throttles to once per 2s, so an exception
    # can otherwise lose the last few hits - and the next run then fires
    # blind, which is how a ban happens.
    try:
        if verify and candidates:
            with Cache() as cache, TradeClient(cfg.league, limiter, status=status) as trade:
                dismissed = cache.dismissed_ids()
                candidates = [c for c in candidates if c.item.id not in dismissed]

                say("Fetching currency rates...")
                trade.load_rates()

                checked = 0
                for c in candidates:
                    cached = cache.get_check(c.item.id, cfg.league, cache_hours)
                    if cached:
                        c.market = MarketResult(
                            total=cached.total,
                            query_url=cached.query_url,
                            filters_used=cached.payload.get("filters") or [],
                            # Caveats on what the price means, not trivia about
                            # how it was fetched: a widened query priced the
                            # strongest mod alone. Dropping them here made the
                            # cached report claim more than the live one did.
                            # Absent on rows predating this - see cache.py.
                            relaxed=bool(cached.payload.get("relaxed")),
                            tightened=bool(cached.payload.get("tightened")),
                        )
                        # Restore the numbers the properties would otherwise derive.
                        c.market.listings = _listings_from_payload(cached.payload)
                        c.from_cache = True
                        report.cache_hits += 1
                        # Costs no API call, and salvages checks stored before
                        # feature logging existed - or under an older ruleset,
                        # since the verdict in hand was scored by the current
                        # one against the same unchanged item.
                        cache.label_features(c.item.id, cfg.league, _features(c.verdict, ruleset))
                        continue
                    if checked >= cap:
                        report.notes.append(
                            f"Stopped market checks at the {cap}-item cap; "
                            f"{len(candidates) - checked} candidates scored by rules only."
                        )
                        break
                    left = trade.budget()
                    say(f"Checking market for {c.item.label} ({checked + 1}/{min(cap, len(candidates))})"
                        + (f" - {left[0]} searches left in window" if left else ""))
                    c.market = trade.price_check(c.item, c.verdict)
                    # Never cache a failure. `error` is not persisted, so a stored
                    # failure reads back as total=0 - which `interest` scores at 48
                    # as "no comparable listings, possibly scarce". One ConnectError
                    # would promote a junk item into the top results and not be
                    # retried for the full cache lifetime.
                    if not c.market.error:
                        cache.put_check(
                            c.item.id, cfg.league, c.market, _features(c.verdict, ruleset)
                        )
                    checked += 1
                report.market_checks = checked
    finally:
        limiter.save(force=True)


    candidates.sort(key=lambda c: -c.interest)
    report.candidates = candidates
    return report


def _features(verdict: Verdict, ruleset: Ruleset) -> dict:
    """The item side of a price observation, for later calibration.

    Every scan already pays for real market data; recording what the item
    actually was turns that into evidence about which bases and mods carry
    value, instead of the hand-authored priors the ruleset uses today.

    Mods are stored in template form alongside the rolled value, because the
    template is the unit rules match on and the value is what thresholds
    compare. ``rules_hit`` is kept so a rule's score can later be checked
    against what the items it fired on turned out to be worth - and it is
    stamped with ``ruleset``, because a rule id only means something in the
    company of the ruleset that defined it. See ``triage.fingerprint``.
    """
    it = verdict.item
    return {
        "ruleset": ruleset.fingerprint,
        "base_type": it.base_type,
        "category": it.category,
        "ilvl": it.ilvl,
        "influences": list(it.influences),
        "corrupted": it.corrupted,
        "fractured": it.fractured,
        "synthesised": it.synthesised,
        "sockets": it.sockets,
        "links": it.links,
        "quality": it.quality,
        "triage_score": verdict.score,
        "rules_hit": [h.rule_id for h in verdict.hits],
        "mods": [
            {"t": m.template, "id": m.stat_id, "v": m.value, "d": m.domain} for m in it.mods
        ],
    }


def _listings_from_payload(payload: dict) -> list:
    from .trade import Listing

    out = []
    for row in payload.get("listings") or []:
        out.append(
            Listing(
                price_amount=float(row.get("amount") or 0),
                price_currency=str(row.get("currency") or ""),
                chaos_value=row.get("chaos"),
                account="",
                item_name=str(row.get("name") or ""),
            )
        )
    return out
