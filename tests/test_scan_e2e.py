"""End-to-end scan through the real pipeline with the network stubbed out."""

import json

import fixtures as F
import pytest

from poescan import report as report_mod
from poescan import scanner
from poescan.config import Config
from poescan.items import from_stash_json
from poescan.scanner import ScanReport, scan, select_tabs
from poescan.stash import Tab
from poescan.trade import Listing, MarketResult
from poescan.triage import Ruleset


class FakeStash:
    """Stands in for StashClient; returns fixture items from two tabs."""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def flat_tabs(self):
        return [
            Tab(id="t1", name="dump1", type="QuadStash", index=0),
            Tab(id="t2", name="maps", type="PremiumStash", index=1),
        ]

    def tab_items(self, tab, index):
        if tab.name != "dump1":
            return []
        return [
            from_stash_json(raw, index, tab_name=tab.name, tab_id=tab.id)
            for raw in F.SHOULD_FLAG + F.SHOULD_REJECT
        ]


class FakeTrade:
    """Deterministic market answers keyed on item id."""

    ANSWERS = {
        "tailwind-boots": (4, [180.0, 200.0, 240.0]),
        "plus-one-wand": (9, [60.0, 75.0, 90.0]),
        "minus-res-helm": (2, [300.0, 420.0]),
        "mana-cost-ring": (30, [25.0, 30.0, 45.0]),
        "fractured-ring": (140, [2.0, 3.0, 4.0]),
        "chaos-amulet": (600, [1.0, 1.0, 2.0]),
    }

    def __init__(self, *a, **kw):
        self.checks = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def load_rates(self):
        pass

    def budget(self):
        return (20, 300.0)

    def price_check(self, item, verdict, sample=10):
        self.checks += 1
        total, prices = self.ANSWERS.get(item.id, (0, []))
        r = MarketResult(total=total, query_url=f"https://example.test/{item.id}")
        r.filters_used = ["life >= 80"]
        r.listings = [Listing(p, "chaos", p, "seller", item.label) for p in prices]
        return r


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    monkeypatch.setattr(scanner, "StashClient", FakeStash)
    monkeypatch.setattr(scanner, "TradeClient", FakeTrade)
    # Keep the cache out of the user's real home directory.
    import poescan.cache as cache_mod

    monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "cache.sqlite")
    yield


@pytest.fixture
def cfg():
    return Config(client_id="x", league="Allflame")


# Modelled on a real stash: bare-number tab names, duplicates, and special tabs.
REAL_TABS = [
    Tab(id="a", name="Currency", type="CurrencyStash", index=0),
    Tab(id="b", name="Maps", type="MapStash", index=1),
    Tab(id="c", name="To sell", type="PremiumStash", index=12),
    Tab(id="d", name="Misc", type="PremiumStash", index=13),
    Tab(id="e", name="~price 20 chaos", type="QuadStash", index=16),
    Tab(id="f", name="2", type="PremiumStash", index=20),
    Tab(id="g", name="2", type="PremiumStash", index=33),
    Tab(id="h", name="22", type="PremiumStash", index=40),
]


def test_exact_name_match_beats_substring():
    """'2' must not drag in '22' or '~price 20 chaos'."""
    got = select_tabs(REAL_TABS, ["2"])
    assert [t.index for t in got] == [20, 33]  # both tabs literally named "2"


def test_index_selector_disambiguates_duplicate_names():
    assert [t.index for t in select_tabs(REAL_TABS, ["#33"])] == [33]


def test_bare_number_is_a_name_not_an_index():
    """Index 2 is Maps-adjacent; the user means the tabs named '2'."""
    got = select_tabs(REAL_TABS, ["2"])
    assert all(t.name == "2" for t in got)


def test_substring_used_only_when_nothing_matches_exactly():
    got = select_tabs(REAL_TABS, ["price"])
    assert [t.name for t in got] == ["~price 20 chaos"]


def test_multiple_selectors_do_not_duplicate():
    got = select_tabs(REAL_TABS, ["Misc", "misc", "#13"])
    assert len(got) == 1


def test_empty_selection_skips_non_gear_tabs():
    got = select_tabs(REAL_TABS, [])
    names = [t.name for t in got]
    assert "Currency" not in names and "Maps" not in names
    assert "Misc" in names and "To sell" in names


def test_special_tab_can_still_be_named_explicitly():
    assert [t.name for t in select_tabs(REAL_TABS, ["Currency"])] == ["Currency"]


def test_scan_surfaces_every_valuable_item(stubbed, cfg, index):
    """Triage is a coarse filter now, so the guarantee is no false negatives:
    everything valuable must reach the market check. Ordinary items coming
    along too is expected and is what the market check is for."""
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(), tabs_wanted=["dump1"])
    assert rep.total_items == len(F.SHOULD_FLAG) + len(F.SHOULD_REJECT)
    flagged = {c.item.id for c in rep.candidates}
    assert {raw["id"] for raw in F.SHOULD_FLAG} <= flagged


def test_scan_never_surfaces_vetoed_items(stubbed, cfg, index):
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(), tabs_wanted=["dump1"])
    flagged = {c.item.id for c in rep.candidates}
    for raw in (F.JUNK_RING, F.LOW_ILVL_RING, F.UNIDENTIFIED):
        assert raw["id"] not in flagged


def test_valuable_items_outrank_ordinary_ones_after_market_check(stubbed, cfg, index):
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(), tabs_wanted=["dump1"])
    order = [c.item.id for c in rep.candidates]
    # FakeTrade prices minus-res-helm at 300c+ and chaos-amulet at ~1c.
    assert order.index("minus-res-helm") < order.index("chaos-amulet")


def test_scan_ranks_by_market_signal(stubbed, cfg, index):
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(), tabs_wanted=["dump1"])
    order = [c.item.id for c in rep.candidates]
    # A thin market at 300c+ must outrank a flooded market at 1c.
    assert order.index("minus-res-helm") < order.index("chaos-amulet")
    assert order.index("tailwind-boots") < order.index("chaos-amulet")


def test_flooded_market_pushes_item_below_its_triage_score(stubbed, cfg, index):
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(), tabs_wanted=["dump1"])
    amulet = next(c for c in rep.candidates if c.item.id == "chaos-amulet")
    assert amulet.interest < amulet.verdict.score


def test_no_verify_skips_the_trade_api(stubbed, cfg, index):
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(),
               tabs_wanted=["dump1"], verify=False)
    assert rep.market_checks == 0
    assert all(c.market is None for c in rep.candidates)


def test_max_checks_caps_api_usage(stubbed, cfg, index):
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(),
               tabs_wanted=["dump1"], max_checks=2)
    assert rep.market_checks == 2
    assert any("cap" in n for n in rep.notes)


def test_second_scan_reuses_cached_checks(stubbed, cfg, index):
    kw = dict(index=index, ruleset=Ruleset.load(), tabs_wanted=["dump1"])
    first = scan(cfg, token=None, **kw)
    second = scan(cfg, token=None, **kw)
    assert first.market_checks > 0
    assert second.market_checks == 0
    assert second.cache_hits == first.market_checks


def test_fresh_ignores_cache(stubbed, cfg, index):
    kw = dict(index=index, ruleset=Ruleset.load(), tabs_wanted=["dump1"])
    scan(cfg, token=None, **kw)
    again = scan(cfg, token=None, cache_hours=0, **kw)
    assert again.cache_hits == 0
    assert again.market_checks > 0


def test_unmatched_tab_name_reports_available_tabs(stubbed, cfg, index):
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(), tabs_wanted=["nope"])
    assert rep.candidates == []
    assert any("dump1" in n for n in rep.notes)


def test_report_renders_html(stubbed, cfg, index, tmp_path):
    rep = scan(cfg, token=None, index=index, ruleset=Ruleset.load(), tabs_wanted=["dump1"])
    out = report_mod.render(rep, tmp_path / "r.html")
    html = out.read_text()
    assert "Stride Two-Toned Boots" in html  # apostrophe is escaped by autoescape
    assert "col 1, row 1" in html            # stash position is shown
    assert "https://example.test/" in html    # trade link is clickable
    assert "Tailwind" in html                 # mods are listed
    assert "dump1" in html                    # which tab to look in


def test_report_handles_empty_scan(tmp_path):
    out = report_mod.render(ScanReport(league="Allflame"), tmp_path / "empty.html")
    assert "Nothing crossed the threshold" in out.read_text()


def test_report_escapes_item_text(tmp_path, index):
    """Item names come from the API; they must not be able to inject markup."""
    rep = ScanReport(league="Allflame")
    from poescan.scanner import Candidate
    from poescan.triage import assess

    it = from_stash_json(F.item(name="<script>x</script>"), index)
    rep.candidates = [Candidate(verdict=assess(it, Ruleset.load()))]
    html = report_mod.render(rep, tmp_path / "x.html").read_text()
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


# -- diagnose ---------------------------------------------------------------


def test_diagnose_reports_unknown_categories_and_unresolved_mods(monkeypatch, capsys, index):
    """The pre-flight check must surface the two silent-failure modes:
    items whose category was not recognised, and mods that map to no stat."""
    from poescan import cli
    from poescan.config import Config

    planted = [
        F.item(id="weird", icon="", baseType="Mystery Slab",
               explicitMods=["+5 to Completely Invented Stat"]),
        *F.SHOULD_FLAG,
    ]

    class Stash(FakeStash):
        def tab_items(self, tab, index):
            return [from_stash_json(r, index, tab_name=tab.name) for r in planted]

    monkeypatch.setattr(cli, "open_stash", lambda cfg, token, limiter: Stash())
    monkeypatch.setattr(cli, "_auth", lambda cfg: None)
    monkeypatch.setattr(
        cli.Config, "load", classmethod(lambda c: Config(client_id="x", league="Allflame"))
    )

    class Args:
        tabs = "dump1"
        league = None
        rules = None

    assert cli.cmd_diagnose(Args()) == 0
    out = capsys.readouterr().out
    assert "Mystery Slab" in out                    # unclassified base surfaced
    assert "Invented Stat" in out                   # unresolved mod surfaced
    assert "Triage score distribution" in out
    assert "would be promoted" in out


def test_diagnose_makes_no_trade_api_calls(monkeypatch, index):
    """It must stay free - the whole point is running it before spending budget."""
    from poescan import cli, trade
    from poescan.config import Config

    def boom(*a, **k):
        raise AssertionError("diagnose must not touch the trade API")

    monkeypatch.setattr(trade.TradeClient, "price_check", boom)
    monkeypatch.setattr(cli, "open_stash", lambda cfg, token, limiter: FakeStash())
    monkeypatch.setattr(cli, "_auth", lambda cfg: None)
    monkeypatch.setattr(
        cli.Config, "load", classmethod(lambda c: Config(client_id="x", league="Allflame"))
    )

    class Args:
        tabs = "dump1"
        league = None
        rules = None

    assert cli.cmd_diagnose(Args()) == 0
