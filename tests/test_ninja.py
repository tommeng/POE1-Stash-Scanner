"""poe.ninja base-value import.

The failure this module exists to prevent is subtle: passing the URL slug
("allflame") instead of the league id ("Allflame") returns HTTP 200 with an
empty result rather than an error, so a wrong parameter is indistinguishable
from a league with no economy. Several tests here pin that it is now loud.
"""

import json
import re

import pytest

from poescan import ninja


def _row(name, **kw):
    row = {
        "name": name,
        "itemType": "Ring",
        "variant": "Shaper",
        "levelRequired": 86,
        "chaosValue": 20.0,
        "divineValue": 0.1,
        "count": 40,
        "listingCount": 40,
    }
    row.update(kw)
    return row


ROWS = [
    _row("Opal Ring", chaosValue=11.0, count=39, listingCount=39),
    _row("Vermillion Ring", chaosValue=22.0, count=72, listingCount=72),
    # count saturates at 399 while the real market is far larger.
    _row("Two-Stone Ring", chaosValue=2.0, count=399, listingCount=998),
    _row("Helical Ring", chaosValue=30510.0, count=1, listingCount=1),
    _row("Astral Plate", itemType="Body Armour", chaosValue=50.0),
    _row("Elder Ring", variant="Shaper/Elder", chaosValue=80.0, count=30),
    # Uninfluenced rows omit `variant` entirely - the string "None" never appears.
    _row("Iron Ring", chaosValue=1.0, count=152, listingCount=152),
    _row("Coral Ring", levelRequired=80, chaosValue=1.0, count=20),
]
del ROWS[6]["variant"]


LEAGUES = [{"id": "Allflame"}, {"id": "Standard"}]

LEAGUES_URL = "https://poe.ninja/poe1/api/economy/leagues"
OVERVIEW_URL = "https://poe.ninja/poe1/api/economy/stash/current/item/overview"


def serve(calls=None, lines=None, leagues=None):
    """A `_get` stub that answers both endpoints, recording urls into `calls`.

    Dispatching on the url rather than answering everything alike is what lets a
    test say *which* request was made - the whole of issue #16 is one endpoint
    being called when it should not be.
    """
    rows = ROWS if lines is None else lines

    def _get(url, params=None):
        if calls is not None:
            calls.append(url)
        return (LEAGUES if leagues is None else leagues) if url.endswith("/leagues") \
            else {"lines": rows}

    return _get


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Serve the rows without touching the network or the real data dir."""
    monkeypatch.setattr(ninja, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ninja, "_get", serve())
    return ROWS


def test_rows_are_normalised(stubbed):
    opal = next(v for v in ninja.base_values("Allflame") if v.name == "Opal Ring")
    assert (opal.item_type, opal.influence, opal.ilvl) == ("Ring", "Shaper", 86)
    assert opal.chaos == 11.0 and opal.samples == 39


def test_an_absent_variant_key_means_uninfluenced(stubbed):
    """poe.ninja omits `variant` rather than sending the string "None" - which
    appears zero times in the real 19448-row payload."""
    iron = next(v for v in ninja.base_values("Allflame") if v.name == "Iron Ring")
    assert iron.influence is None
    assert iron.influences == ()


def test_samples_and_listings_are_kept_apart(stubbed):
    """`count` is a capped sample (399 max), `listingCount` is the market. Reading
    one as the other reintroduces the saturating-denominator mistake."""
    ts = next(v for v in ninja.base_values("Allflame") if v.name == "Two-Stone Ring")
    assert ts.samples == 399
    assert ts.listings == 998


def test_a_compound_influence_answers_its_parts(stubbed):
    """A Shaper/Elder base is still a Shaper base; matching the whole string
    silently dropped 492 rows of the real payload."""
    names = {v.name for v in ninja.base_values("Allflame", influence="Shaper")}
    assert "Elder Ring" in names
    assert {v.name for v in ninja.base_values("Allflame", influence="Elder")} == {"Elder Ring"}


def test_an_unknown_influence_is_rejected_not_silently_empty(stubbed):
    with pytest.raises(ninja.NinjaError, match="Unknown influence"):
        ninja.base_values("Allflame", influence="shapper")


def test_an_unknown_slot_is_rejected_not_silently_empty(stubbed):
    """An empty table is indistinguishable from a real empty result."""
    with pytest.raises(ninja.NinjaError, match="Unknown item type"):
        ninja.base_values("Allflame", item_type="Rings")


def test_filters_are_case_insensitive_like_resolve_league(stubbed):
    assert ninja.base_values("Allflame", item_type="ring", influence="shaper")


def test_a_stale_cache_is_refetched(stubbed, monkeypatch, tmp_path):
    """Without this the importer would serve last league's prices forever."""
    import os, time as _t

    ninja.base_values("Allflame")
    cached = next(tmp_path.glob("ninja-basetypes-*.json"))
    old = _t.time() - (ninja.TTL_SECONDS + 60)
    os.utime(cached, (old, old))

    calls = []
    monkeypatch.setattr(ninja, "_get", serve(calls))
    ninja.base_values("Allflame")
    assert OVERVIEW_URL in calls


def test_each_league_gets_its_own_cache(stubbed, monkeypatch, tmp_path):
    """A shared key would serve one league's prices for another."""
    ninja.base_values("Allflame")
    other = [_row("Opal Ring", chaosValue=999.0, count=10)]
    monkeypatch.setattr(ninja, "_get", serve(lines=other))
    got = ninja.base_values("Standard")
    assert [v.chaos for v in got] == [999.0]
    assert next(v for v in ninja.base_values("Allflame") if v.name == "Opal Ring").chaos == 11.0


def test_values_are_sorted_most_valuable_first(stubbed):
    values = [v.chaos for v in ninja.base_values("Allflame")]
    assert values == sorted(values, reverse=True)


def test_filters_compose(stubbed):
    got = ninja.base_values(
        "Allflame", item_type="Ring", influence="Shaper", ilvl_min=84, min_samples=5
    )
    names = {v.name for v in got}
    assert "Opal Ring" in names
    assert "Astral Plate" not in names       # wrong slot
    assert "Iron Ring" not in names          # uninfluenced
    assert "Coral Ring" not in names         # ilvl 80
    assert "Helical Ring" not in names       # one listing


def test_uninfluenced_can_be_selected_explicitly(stubbed):
    got = ninja.base_values("Allflame", influence="None")
    assert {v.name for v in got} == {"Iron Ring"}


def test_a_single_listing_is_not_confident(stubbed):
    """One listed Helical Ring set its own row to 30510c."""
    helical = next(v for v in ninja.base_values("Allflame") if v.name == "Helical Ring")
    assert helical.chaos > 30000
    assert not helical.confident
    assert next(v for v in ninja.base_values("Allflame") if v.name == "Opal Ring").confident


def test_an_empty_payload_for_a_real_league_still_says_so(monkeypatch, tmp_path):
    """200-with-nothing otherwise reads as 'this league has no economy data'."""
    monkeypatch.setattr(ninja, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ninja, "_get", serve(lines=[]))
    with pytest.raises(ninja.NinjaError, match="not the URL slug"):
        ninja.fetch_base_types("Allflame")


def test_the_slug_never_reaches_the_api(monkeypatch, tmp_path):
    """Resolving on the miss closes the slug trap by construction.

    `fetch_base_types` used to forward whatever it was handed, so "allflame"
    reached poe.ninja and came back 200-with-nothing. It now resolves first, so
    the request carries the id no matter which spelling the caller used.
    """
    monkeypatch.setattr(ninja, "DATA_DIR", tmp_path)
    sent = []

    def _get(url, params=None):
        sent.append((url, dict(params or {})))
        return LEAGUES if url.endswith("/leagues") else {"lines": ROWS}

    monkeypatch.setattr(ninja, "_get", _get)
    ninja.fetch_base_types("allflame")

    overview = next(p for u, p in sent if u == OVERVIEW_URL)
    assert overview["league"] == "Allflame", "the id, not the slug the caller passed"


def test_a_warm_cache_makes_no_requests_at_all(stubbed, monkeypatch, tmp_path):
    """Issue #16: resolving eagerly cost one league-list request every call.

    The base-type cache was already honoured, so the command looked cached while
    still going to the network - which is exactly what the README says it does
    not do. A warm cache must be silent, not merely cheap.
    """
    ninja.base_values("Allflame")  # populate
    calls = []
    monkeypatch.setattr(ninja, "_get", serve(calls))

    ninja.base_values("Allflame")
    ninja.item_types("Allflame")
    ninja.influences("Allflame")

    assert calls == [], f"warm cache should touch nothing, called {calls}"


def test_a_cache_miss_does_resolve_the_league(stubbed, monkeypatch, tmp_path):
    """The other half: laziness must not become never."""
    calls = []
    monkeypatch.setattr(ninja, "_get", serve(calls))
    ninja.base_values("Allflame")
    assert calls == [LEAGUES_URL, OVERVIEW_URL]


def test_league_resolves_case_insensitively(monkeypatch):
    monkeypatch.setattr(ninja, "_get", lambda url, params=None: [{"id": "Allflame"}])
    assert ninja.resolve_league("allflame") == "Allflame"
    assert ninja.resolve_league("  ALLFLAME ") == "Allflame"


def test_an_unknown_league_lists_what_is_available(monkeypatch):
    monkeypatch.setattr(
        ninja, "_get", lambda url, params=None: [{"id": "Allflame"}, {"id": "Standard"}]
    )
    with pytest.raises(ninja.NinjaError, match="Allflame, Standard"):
        ninja.resolve_league("Settlers")


def test_results_are_cached_to_disk(stubbed, monkeypatch, tmp_path):
    ninja.base_values("Allflame")
    calls = []
    monkeypatch.setattr(ninja, "_get", serve(calls))
    ninja.base_values("Allflame")
    assert calls == [], "second call should be served from the 24h cache"


def test_refresh_bypasses_the_cache(stubbed, monkeypatch, tmp_path):
    ninja.base_values("Allflame")
    calls = []
    monkeypatch.setattr(ninja, "_get", serve(calls))
    ninja.base_values("Allflame", force=True)
    assert OVERVIEW_URL in calls


def test_a_corrupt_cache_is_refetched(stubbed, tmp_path):
    ninja.base_values("Allflame")
    cached = next(tmp_path.glob("ninja-basetypes-*.json"))
    cached.write_text("{not json")
    assert ninja.base_values("Allflame"), "should fall through to a re-fetch"


def test_item_types_lists_the_slots(stubbed):
    assert ninja.item_types("Allflame") == ["Body Armour", "Ring"]


def test_influences_lists_components_not_compounds(stubbed):
    """So `--influences` shows what you can actually pass."""
    assert ninja.influences("Allflame") == ["Elder", "Shaper"]


def test_leagues_rejects_an_unexpected_shape(monkeypatch):
    monkeypatch.setattr(ninja, "_get", lambda url, params=None: {"not": "a list"})
    with pytest.raises(ninja.NinjaError, match="unexpected shape"):
        ninja.leagues()


def test_a_cache_of_the_wrong_shape_is_refetched(stubbed, tmp_path):
    """Valid JSON, wrong type - would otherwise blow up inside row.get()."""
    ninja.base_values("Allflame")
    next(tmp_path.glob("ninja-basetypes-*.json")).write_text('{"lines": []}')
    assert ninja.base_values("Allflame")


def test_transport_failure_is_wrapped(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setattr(ninja, "DATA_DIR", tmp_path)

    def boom(*a, **kw):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(ninja.NinjaError, match="Could not reach poe.ninja"):
        ninja.leagues()


def test_the_cached_payload_is_the_raw_rows(stubbed, tmp_path):
    """Kept raw so a future reader can re-derive fields we do not surface yet."""
    ninja.base_values("Allflame")
    cached = json.loads(next(tmp_path.glob("ninja-basetypes-*.json")).read_text())
    assert cached == ROWS


# -- the command ------------------------------------------------------------
#
# cmd_base_values had no test, which is how a default of --min-samples 5 came to
# make the "thin row" display branches unreachable while the footer still
# reported on them.


def _args(**kw):
    import types

    base = dict(slot=None, slots=False, influences=False, influence=None, ilvl=None,
                min_samples=5, top=30, league="Allflame", refresh=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def stubbed_cli(stubbed, monkeypatch):
    monkeypatch.setattr(ninja, "resolve_league", lambda name: "Allflame")
    from poescan import cli

    return cli


def test_command_renders(stubbed_cli):
    assert stubbed_cli.cmd_base_values(_args(slot="Ring")) == 0


def test_command_lists_slots_and_influences(stubbed_cli, capsys):
    assert stubbed_cli.cmd_base_values(_args(slots=True)) == 0
    assert "Ring" in capsys.readouterr().out
    assert stubbed_cli.cmd_base_values(_args(influences=True)) == 0
    out = capsys.readouterr().out
    assert "Shaper" in out and "Elder" in out


def test_command_reports_a_bad_filter_rather_than_an_empty_table(stubbed_cli, capsys):
    assert stubbed_cli.cmd_base_values(_args(slot="Rings")) == 1
    assert "Unknown item type" in capsys.readouterr().out


def _matched(out: str) -> int:
    """The "N rows matched" figure, with rich's escapes stripped."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    return int(re.search(r"(\d+) rows matched", plain).group(1))


def test_command_hides_thin_rows_by_default(stubbed_cli, capsys):
    """The one-sample Helical Ring is excluded unless asked for. Asserting on
    the name would pass spuriously - the footer mentions it either way."""
    stubbed_cli.cmd_base_values(_args(slot="Ring", min_samples=5))
    strict = _matched(capsys.readouterr().out)
    stubbed_cli.cmd_base_values(_args(slot="Ring", min_samples=0))
    loose = _matched(capsys.readouterr().out)
    assert loose > strict


def test_command_formats_big_values_in_divine(stubbed_cli, capsys):
    """Previously unreachable: the divine branch lived only behind a filter that
    could never be false, so a 30510c row printed as chaos."""
    stubbed_cli.cmd_base_values(_args(slot="Ring", min_samples=0))
    assert "div" in capsys.readouterr().out
