"""poe.ninja base-value import.

The failure this module exists to prevent is subtle: passing the URL slug
("allflame") instead of the league id ("Allflame") returns HTTP 200 with an
empty result rather than an error, so a wrong parameter is indistinguishable
from a league with no economy. Several tests here pin that it is now loud.
"""

import json

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
    }
    row.update(kw)
    return row


ROWS = [
    _row("Opal Ring", chaosValue=11.0, count=39),
    _row("Vermillion Ring", chaosValue=22.0, count=72),
    _row("Two-Stone Ring", chaosValue=2.0, count=399),
    _row("Helical Ring", chaosValue=30510.0, count=1),          # one listing, absurd price
    _row("Astral Plate", itemType="Body Armour", chaosValue=50.0),
    _row("Iron Ring", variant="None", chaosValue=1.0, count=152),
    _row("Coral Ring", levelRequired=80, chaosValue=1.0, count=20),
]


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Serve the rows without touching the network or the real data dir."""
    monkeypatch.setattr(ninja, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ninja, "_get", lambda url, params=None: {"lines": ROWS})
    return ROWS


def test_rows_are_normalised(stubbed):
    opal = next(v for v in ninja.base_values("Allflame") if v.name == "Opal Ring")
    assert (opal.item_type, opal.influence, opal.ilvl) == ("Ring", "Shaper", 86)
    assert opal.chaos == 11.0 and opal.count == 39


def test_uninfluenced_variant_becomes_none(stubbed):
    """poe.ninja spells it as the string "None"."""
    iron = next(v for v in ninja.base_values("Allflame") if v.name == "Iron Ring")
    assert iron.influence is None


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


def test_the_slug_trap_produces_a_real_error(monkeypatch, tmp_path):
    """The wrong league form answers 200 with nothing, which otherwise reads as
    'this league has no economy data'."""
    monkeypatch.setattr(ninja, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ninja, "_get", lambda url, params=None: {"lines": []})
    with pytest.raises(ninja.NinjaError, match="not the URL slug"):
        ninja.fetch_base_types("allflame")


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
    monkeypatch.setattr(ninja, "_get", lambda url, params=None: calls.append(url) or {"lines": ROWS})
    ninja.base_values("Allflame")
    assert calls == [], "second call should be served from the 24h cache"


def test_refresh_bypasses_the_cache(stubbed, monkeypatch, tmp_path):
    ninja.base_values("Allflame")
    calls = []
    monkeypatch.setattr(ninja, "_get", lambda url, params=None: calls.append(url) or {"lines": ROWS})
    ninja.base_values("Allflame", force=True)
    assert len(calls) == 1


def test_a_corrupt_cache_is_refetched(stubbed, tmp_path):
    ninja.base_values("Allflame")
    cached = next(tmp_path.glob("ninja-basetypes-*.json"))
    cached.write_text("{not json")
    assert ninja.base_values("Allflame"), "should fall through to a re-fetch"


def test_item_types_lists_the_slots(stubbed):
    assert ninja.item_types("Allflame") == ["Body Armour", "Ring"]


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
