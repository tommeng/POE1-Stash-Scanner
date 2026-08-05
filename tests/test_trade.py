"""Trade query construction and market-signal scoring."""

import fixtures as F
import pytest

from poescan.items import from_stash_json
from poescan.trade import Listing, MarketResult, TradeClient
from poescan.triage import Ruleset, assess


@pytest.fixture(scope="session")
def ruleset():
    return Ruleset.load()


@pytest.fixture
def client():
    c = TradeClient("Allflame")
    yield c
    c.close()


def _query(client, raw, index, ruleset):
    it = from_stash_json(raw, index)
    query, _described = client.build_query(it, assess(it, ruleset))
    return it, query


def test_query_filters_by_category_and_rarity(client, index, ruleset):
    _, q = _query(client, F.PLUS_ONE_WAND, index, ruleset)
    tf = q["query"]["filters"]["type_filters"]["filters"]
    assert tf["category"]["option"] == "weapon.wand"
    assert tf["rarity"]["option"] == "rare"


def test_query_includes_influence(client, index, ruleset):
    _, q = _query(client, F.TAILWIND_BOOTS, index, ruleset)
    misc = q["query"]["filters"]["misc_filters"]["filters"]
    assert misc["hunter_item"]["option"] == "true"


def test_query_uses_the_mods_that_earned_the_score(client, index, ruleset):
    _, q = _query(client, F.PLUS_ONE_WAND, index, ruleset)
    ids = [f["id"] for f in q["query"]["stats"][0]["filters"]]
    # +1 to Level of all Spell Skill Gems is the reason this wand matters.
    assert "explicit.stat_124131830" in ids


def test_query_relaxes_values_by_slack(client, index, ruleset):
    it = from_stash_json(F.MINUS_RES_HELM, index)
    q, _ = client.build_query(it, assess(it, ruleset), slack=0.9)
    filters = {f["id"]: f.get("value", {}).get("min") for f in q["query"]["stats"][0]["filters"]}
    life = filters.get("pseudo.pseudo_total_life")
    assert life is not None and life < 99  # relaxed below the item's own 99


def test_query_is_capped_to_stay_answerable(client, index, ruleset):
    """Over-specified queries return zero results and teach us nothing."""
    _, q = _query(client, F.CHAOS_RES_AMULET, index, ruleset)
    assert len(q["query"]["stats"][0]["filters"]) <= 4


def test_query_excludes_unpriced_listings(client, index, ruleset):
    """Unpriced listings must be negotiated in whisper, so their asking price
    is unknown; including them would inflate the listing count for free."""
    _, q = _query(client, F.PLUS_ONE_WAND, index, ruleset)
    sale = q["query"]["filters"]["trade_filters"]["filters"]["sale_type"]
    assert sale == {"option": "priced"}


def test_query_sorts_by_price_ascending(client, index, ruleset):
    _, q = _query(client, F.PLUS_ONE_WAND, index, ruleset)
    assert q["sort"] == {"price": "asc"}


def test_prices_against_instant_buyout_by_default(client, index, ruleset):
    """In-person listings are largely bait: the same wand shows a 4c median
    across 54 in-person listings vs 90c across 4126 instant-buyout ones."""
    _, q = _query(client, F.PLUS_ONE_WAND, index, ruleset)
    assert q["query"]["status"]["option"] == "securable"


def test_status_is_overridable(index, ruleset):
    c = TradeClient("Allflame", status="online")
    it = from_stash_json(F.PLUS_ONE_WAND, index)
    q, _ = c.build_query(it, assess(it, ruleset))
    c.close()
    assert q["query"]["status"]["option"] == "online"


def test_unknown_status_falls_back_to_default():
    c = TradeClient("Allflame", status="not-a-real-status")
    assert c.status == "securable"
    c.close()


def test_unknown_category_is_omitted_not_guessed(client, index, ruleset):
    it = from_stash_json(F.item(icon="", baseType="Mystery Thing"), index)
    q, _ = client.build_query(it, assess(it, ruleset))
    assert "category" not in q["query"]["filters"]["type_filters"]["filters"]


def test_max_stats_narrows_the_filter_set(client, index, ruleset):
    it = from_stash_json(F.PLUS_ONE_WAND, index)
    wide, _ = client.build_query(it, assess(it, ruleset), max_stats=1)
    assert len(wide["query"]["stats"][0]["filters"]) == 1


def test_negative_mods_are_bounded_above_not_below(client, index, ruleset):
    """-7 mana cost is better the more negative it is, so it needs a max."""
    it = from_stash_json(F.MANA_COST_RING, index)
    q, _ = client.build_query(it, assess(it, ruleset), slack=0.9)
    mana = next(
        f for f in q["query"]["stats"][0]["filters"] if f["id"] == "explicit.stat_3736589033"
    )
    assert "min" not in mana["value"]
    # Slack must move the bound toward zero (wider), not away from it.
    assert -7.0 < mana["value"]["max"] < 0


def test_more_slack_widens_a_negative_bound(client, index, ruleset):
    it = from_stash_json(F.MANA_COST_RING, index)
    v = assess(it, ruleset)
    tight, _ = client.build_query(it, v, slack=0.95)
    loose, _ = client.build_query(it, v, slack=0.5)

    def mana_max(q):
        return next(
            f for f in q["query"]["stats"][0]["filters"]
            if f["id"] == "explicit.stat_3736589033"
        )["value"]["max"]

    assert mana_max(loose) > mana_max(tight)  # closer to zero == more permissive


# -- relaxation retry -------------------------------------------------------


class _FetchResponse:
    """The shape price_check expects back from a listing fetch."""

    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class RecordingClient(TradeClient):
    """Captures searches and replays scripted responses, no network.

    Nothing in this file may touch the live API: the trade endpoints are rate
    limited with a ban rather than a soft 429, so a test suite that reaches
    them spends a budget the user needs and eventually blocks on it.
    """

    def __init__(self, responses, fetch_rows=None):
        super().__init__("Allflame")
        self.responses = list(responses)
        self.fetch_rows = list(fetch_rows or [])
        self.searches = []
        self.fetches = 0

    def _search(self, query):
        self.searches.append(query)
        return self.responses.pop(0), ""

    def _request(self, method, url, policy, **kw):
        """Stub the listing fetch.

        price_check fetches listings whenever a search returns ids, and only
        _search was stubbed here - so every scripted response carrying an id
        sent a live request to GGG on each run of the suite. That spent the
        real 6-hour fetch budget and let the rate limiter stall the whole suite
        for a full 300s window once the allowance ran out.
        """
        self.fetches += 1
        return _FetchResponse({"result": self.fetch_rows})


def test_strict_query_with_no_hits_is_retried_wider(index, ruleset):
    it = from_stash_json(F.PLUS_ONE_WAND, index)
    c = RecordingClient([
        {"id": "q1", "result": [], "total": 0},          # strict: nothing
        {"id": "q2", "result": [], "total": 61},          # wide: hits, but no ids to fetch
    ])
    result = c.price_check(it, assess(it, ruleset))
    c.close()

    assert len(c.searches) == 2
    strict, wide = c.searches
    assert len(wide["query"]["stats"][0]["filters"]) < len(strict["query"]["stats"][0]["filters"])
    assert result.relaxed is True
    assert result.total == 61


def test_no_retry_when_the_strict_query_finds_something(index, ruleset):
    it = from_stash_json(F.PLUS_ONE_WAND, index)
    c = RecordingClient([{"id": "q1", "result": ["a"], "total": 12}])
    result = c.price_check(it, assess(it, ruleset))
    c.close()

    assert len(c.searches) == 1
    assert result.relaxed is False
    assert c.fetches == 1  # and served from the stub, not the live API


def test_the_suite_never_reaches_the_live_trade_api(index, ruleset, monkeypatch):
    """Guard against re-introducing live calls.

    A search that returns ids makes price_check fetch the listings. When only
    _search was stubbed, that fetch went to GGG for real on every run - which
    spends a budget enforced by bans, not 429s. If the fetch stub is ever
    removed, this fails instead of quietly costing the user requests.
    """
    def explode(*a, **kw):
        raise AssertionError("a test tried to reach the live trade API")

    monkeypatch.setattr(TradeClient, "_request", explode)

    it = from_stash_json(F.PLUS_ONE_WAND, index)
    c = RecordingClient(
        [{"id": "q1", "result": ["a"], "total": 12}],
        fetch_rows=[
            {
                "listing": {"price": {"amount": 5, "currency": "chaos"}},
                "item": {"name": "Doom Finger", "typeLine": "Wand"},
            }
        ],
    )
    result = c.price_check(it, assess(it, ruleset))
    c.close()

    assert c.fetches == 1
    assert [listing.price_amount for listing in result.listings] == [5.0]


def test_genuinely_unique_item_stays_at_zero(index, ruleset):
    """If even the widened query finds nothing, report that honestly."""
    it = from_stash_json(F.PLUS_ONE_WAND, index)
    c = RecordingClient([
        {"id": "q1", "result": [], "total": 0},
        {"id": "q2", "result": [], "total": 0},
    ])
    result = c.price_check(it, assess(it, ruleset))
    c.close()

    assert len(c.searches) == 2
    assert result.relaxed is False
    assert result.total == 0
    assert result.summary == "no comparable listings"


# -- currency ---------------------------------------------------------------


def test_to_chaos_converts_known_currency(client):
    client.rates["divine"] = 150.0
    assert client.to_chaos(2, "divine") == 300.0
    assert client.to_chaos(5, "chaos") == 5.0


def test_to_chaos_returns_none_rather_than_guessing(client):
    assert client.to_chaos(3, "mirror") is None


# -- market result ----------------------------------------------------------


def _result(prices, total=None):
    r = MarketResult(total=total if total is not None else len(prices))
    r.listings = [Listing(p, "chaos", p, "acct", "thing") for p in prices]
    return r


def test_market_summary_stats():
    r = _result([1, 2, 3, 40])
    assert r.cheapest == 1
    assert r.median == 2.5


def test_unpriced_listings_are_skipped():
    r = MarketResult(total=2)
    r.listings = [Listing(1, "mirror", None, "a", "x"), Listing(10, "chaos", 10.0, "b", "y")]
    assert r.priced == [10.0]
    assert r.median == 10.0


def test_empty_result_summary():
    assert MarketResult(total=0).summary == "no comparable listings"


# -- interest scoring -------------------------------------------------------


def _candidate(index, ruleset, raw, market):
    from poescan.scanner import Candidate

    it = from_stash_json(raw, index)
    return Candidate(verdict=assess(it, ruleset), market=market)


def test_flooded_cheap_market_demotes(index, ruleset):
    """Cheap and abundant must rank below real value, whatever it scored."""
    junk = _candidate(index, ruleset, F.TAILWIND_BOOTS, _result([3, 4, 4, 5], total=9417))
    good = _candidate(index, ruleset, F.CHAOS_RES_AMULET, _result([60] * 6, total=12))
    assert junk.verdict.score > good.verdict.score  # junk scored higher on rules
    assert junk.interest < good.interest            # market overrules that


def test_midrange_price_is_a_mild_positive(index, ruleset):
    """~90c (the measured +1-spell-gem wand median) is worth listing, not a jackpot."""
    c = _candidate(index, ruleset, F.PLUS_ONE_WAND, _result([80, 90, 100], total=4126))
    assert c.verdict.score < c.interest < c.verdict.score + 40


def test_thin_expensive_market_promotes(index, ruleset):
    # ~380c is the measured instant-buyout median for real Tailwind boots.
    prices = [350, 360, 380, 400, 420, 450]
    c = _candidate(index, ruleset, F.TAILWIND_BOOTS, _result(prices, total=8))
    assert c.market.confident
    assert c.interest >= 100


def test_market_outranks_triage_score(index, ruleset):
    """Measured on a real tab: the top-scoring item was worth 2c while a
    lower-scoring one fetched 65c. Price has to win."""
    high_score_junk = _candidate(
        index, ruleset, F.TAILWIND_BOOTS, _result([1, 2, 2], total=1184)
    )
    low_score_value = _candidate(
        index, ruleset, F.ORDINARY_RING, _result([60, 65, 70, 65, 60, 62], total=6)
    )
    assert high_score_junk.verdict.score > low_score_value.verdict.score
    assert low_score_value.interest > high_score_junk.interest


def test_unchecked_items_fall_back_to_their_triage_score(index, ruleset):
    c = _candidate(index, ruleset, F.TAILWIND_BOOTS, None)
    assert c.interest == c.verdict.score


def test_high_price_from_a_tiny_sample_is_damped(index, ruleset):
    """Three unsold listings at 4 divine are not evidence of value.

    This is the real failure seen in the field: a narrowed query matched three
    coincidentally-similar amulets, none of which had sold.
    """
    thin = _candidate(index, ruleset, F.CHAOS_RES_AMULET, _result([570, 570, 760], total=3))
    thick = _candidate(index, ruleset, F.CHAOS_RES_AMULET, _result([570] * 6, total=8))

    assert not thin.market.confident
    assert thick.market.confident
    # Still positive - just far less certain.
    assert thin.interest < thick.interest


def test_junk_signal_is_not_damped_by_sample_size(index, ruleset):
    """'Thousands listed at 1c' is never a small-sample problem."""
    checked = _candidate(index, ruleset, F.CHAOS_RES_AMULET, _result([1, 1, 1], total=8532))
    unchecked = _candidate(index, ruleset, F.CHAOS_RES_AMULET, None)
    assert checked.interest < unchecked.interest


def test_no_comparables_ranks_mid_pack(index, ruleset):
    """Unknown is not the same as valuable - it sits between junk and value."""
    unknown = _candidate(index, ruleset, F.TAILWIND_BOOTS, MarketResult(total=0))
    junk = _candidate(index, ruleset, F.TAILWIND_BOOTS, _result([1, 1, 1], total=5000))
    value = _candidate(index, ruleset, F.TAILWIND_BOOTS, _result([300] * 6, total=9))
    assert junk.interest < unknown.interest < value.interest


def test_market_error_leaves_score_untouched(index, ruleset):
    m = MarketResult(total=0)
    m.error = "search returned HTTP 503"
    c = _candidate(index, ruleset, F.TAILWIND_BOOTS, m)
    assert c.interest == c.verdict.score


def test_over_broad_query_is_tightened_with_more_mods(index, ruleset):
    """8532 matches means the cheapest listings are the floor, not peers."""
    it = from_stash_json(F.CHAOS_RES_AMULET, index)
    c = RecordingClient([
        {"id": "q1", "result": ["a"], "total": 8532},   # far too common
        {"id": "q2", "result": ["b"], "total": 40},      # narrowed to real peers
    ])
    result = c.price_check(it, assess(it, ruleset))
    c.close()

    assert len(c.searches) == 2
    broad, tight = c.searches
    assert len(tight["query"]["stats"][0]["filters"]) > len(broad["query"]["stats"][0]["filters"])
    assert result.tightened is True
    assert result.total == 40


def test_tightening_keeps_the_broad_answer_if_it_overshoots(index, ruleset):
    it = from_stash_json(F.CHAOS_RES_AMULET, index)
    c = RecordingClient([
        {"id": "q1", "result": ["a"], "total": 8532},
        {"id": "q2", "result": [], "total": 0},   # narrowed too far
    ])
    result = c.price_check(it, assess(it, ruleset))
    c.close()

    assert result.tightened is False
    assert result.total == 8532


def test_reasonable_result_count_is_left_alone(index, ruleset):
    it = from_stash_json(F.CHAOS_RES_AMULET, index)
    c = RecordingClient([{"id": "q1", "result": ["a"], "total": 40}])
    result = c.price_check(it, assess(it, ruleset))
    c.close()

    assert len(c.searches) == 1
    assert result.tightened is False and result.relaxed is False


# -- base type survey -------------------------------------------------------


def test_base_market_isolates_the_base_with_no_stat_filters():
    """The survey's whole point is that the base is the only thing varying."""
    c = RecordingClient([{"id": "q1", "result": ["a"], "total": 2882}])
    result = c.base_market("Opal Ring", ilvl_min=84, price_min=50)
    c.close()

    q = c.searches[0]["query"]
    assert q["type"] == "Opal Ring"
    assert "stats" not in q
    tf = q["filters"]["type_filters"]["filters"]
    assert tf["rarity"]["option"] == "rare"
    assert tf["ilvl"]["min"] == 84
    trade_f = q["filters"]["trade_filters"]["filters"]
    assert trade_f["sale_type"]["option"] == "priced"
    assert trade_f["price"]["min"] == 50
    assert result.total == 2882


def test_base_market_counts_without_fetching():
    """The count is the measurement, so a survey costs one search per base.

    Reading prices instead does not work: sorting ascending across a saturated
    market returns the 1c floor for every base, good or bad.
    """
    c = RecordingClient([{"id": "q1", "result": ["a", "b"], "total": 2882}])
    c.base_market("Opal Ring")
    c.close()
    assert c.fetches == 0


def test_base_market_omits_filters_it_was_not_given():
    c = RecordingClient([{"id": "q1", "result": [], "total": 0}])
    c.base_market("Iron Ring")
    c.close()
    q = c.searches[0]["query"]
    assert "ilvl" not in q["filters"]["type_filters"]["filters"]
    assert "price" not in q["filters"]["trade_filters"]["filters"]


def test_base_value_measures_white_bases_not_rares():
    """A rare's price is its mods; only an unrolled base prices the base.

    Every mod-carrying design inverted the true ranking live, putting Sapphire
    and Iron Rings above Opal. White bases put Opal first, as they should.
    """
    c = RecordingClient(
        [{"id": "q1", "result": ["a"], "total": 13}],
        fetch_rows=[
            {"listing": {"price": {"amount": 18, "currency": "chaos"}}, "item": {}},
            {"listing": {"price": {"amount": 22, "currency": "chaos"}}, "item": {}},
        ],
    )
    result = c.base_value("Opal Ring", ilvl_min=84, influence="shaper")
    c.close()

    q = c.searches[0]["query"]
    assert q["filters"]["type_filters"]["filters"]["rarity"]["option"] == "normal"
    assert q["filters"]["misc_filters"]["filters"]["shaper_item"]["option"] == "true"
    assert q["type"] == "Opal Ring"
    assert "stats" not in q
    assert result.total == 13
    assert result.median == 20.0


def test_base_value_treats_an_unsold_base_as_a_signal():
    """Nobody listing a base unrolled is itself evidence it is worthless."""
    c = RecordingClient([{"id": "q1", "result": [], "total": 0}])
    result = c.base_value("Iron Ring", influence="shaper")
    c.close()
    assert result.total == 0
    assert result.median is None
    assert c.fetches == 0


def test_base_market_reports_a_dead_base_honestly():
    c = RecordingClient([{"id": "q1", "result": [], "total": 0}])
    result = c.base_market("Iron Ring")
    c.close()
    assert result.total == 0
    assert result.summary == "no comparable listings"


def test_summary_flags_a_floor_price_rather_than_implying_a_valuation():
    r = _result([1, 1, 1], total=8532)
    assert "market floor" in r.summary
    assert "market floor" not in _result([1, 1, 1], total=40).summary


def test_tightening_can_use_more_stats_than_the_default_cap(client, index, ruleset):
    it = from_stash_json(F.CHAOS_RES_AMULET, index)
    v = assess(it, ruleset)
    default, _ = client.build_query(it, v)
    tight, _ = client.build_query(it, v, slack=0.95, max_stats=6)
    assert len(default["query"]["stats"][0]["filters"]) <= 4
    assert len(tight["query"]["stats"][0]["filters"]) > len(default["query"]["stats"][0]["filters"])


def test_tightening_skips_mods_nobody_buys_an_item_for(client, index, ruleset):
    """Accuracy Rating outranked real mods by magnitude and poisoned the query."""
    it = from_stash_json(
        F.item(
            baseType="Agate Amulet",
            icon="https://web.poecdn.com/gen/image/WzI1LDE0LHsiZiI6IjJESXRlbXMvQW11bGV0cy9BbXVsZXQ1IiwidyI6MSwiaCI6MSwic2NhbGUiOjF9XQ/x/Amulet5.png",
            ilvl=84,
            explicitMods=[
                "+28% to Global Critical Strike Multiplier",
                "+93 to maximum Life",
                "+54 to Accuracy Rating",
                "+22 to Strength and Intelligence",
                "+28% to Cold Resistance",
            ],
        ),
        index,
    )
    q, described = client.build_query(it, assess(it, ruleset), slack=0.95, max_stats=6)
    joined = " | ".join(described)
    assert "Accuracy Rating" not in joined
    assert "Strength and Intelligence" not in joined
    assert "Critical Strike Multiplier" in joined
