"""Mod normalisation, stat lookup, item parsing and category classification."""

import fixtures as F
import pytest

from poescan.items import art_path, classify, from_stash_json
from poescan.tradedata import normalise, values_in


@pytest.mark.parametrize(
    "text,expected",
    [
        ("+97 to maximum Life", "+# to maximum Life"),
        ("+30% to Lightning Resistance", "+#% to Lightning Resistance"),
        ("Adds 5 to 11 Physical Damage to Attacks", "Adds # to # Physical Damage to Attacks"),
        ("0.22% of Physical Attack Damage Leeched as Mana", "#% of Physical Attack Damage Leeched as Mana"),
        ("-7 to Total Mana Cost of Skills", "-# to Total Mana Cost of Skills"),
        ("36% increased Mana Regeneration Rate", "#% increased Mana Regeneration Rate"),
        ("Curse Enemies with Vulnerability on Hit", "Curse Enemies with Vulnerability on Hit"),
    ],
)
def test_normalise(text, expected):
    assert normalise(text) == expected


def test_normalise_collapses_multiline_mods():
    text = "Passives granting Fire Resistance\nalso grant 8% increased Life"
    assert "\n" not in normalise(text)
    assert normalise(text) == "Passives granting Fire Resistance also grant #% increased Life"


def test_values_in():
    assert values_in("Adds 5 to 11 Physical Damage") == [5.0, 11.0]
    assert values_in("-7 to Total Mana Cost") == [-7.0]
    assert values_in("0.22% leech") == [0.22]
    assert values_in("no numbers here") == []


def test_lookup_resolves_real_stats(index):
    assert index.lookup("+97 to maximum Life") == "explicit.stat_3299347043"
    assert index.lookup("+30% to Lightning Resistance") == "explicit.stat_1671376347"
    assert index.lookup("+1 to Level of all Spell Skill Gems") == "explicit.stat_124131830"


def test_lookup_respects_domain(index):
    implicit = index.lookup("+15% to Fire and Cold Resistances", "implicit")
    explicit = index.lookup("+15% to Fire and Cold Resistances", "explicit")
    assert implicit.startswith("implicit.")
    assert explicit.startswith("explicit.")


def test_lookup_unknown_returns_none(tiny_index):
    assert tiny_index.lookup("+5 to Completely Invented Stat") is None


def test_art_path_decodes_icon():
    assert art_path(F.RING_ICON) == "2DItems/Rings/SapphireRuby"


def test_art_path_survives_garbage():
    assert art_path("") == ""
    assert art_path("https://example.com/not-an-icon.png") == "/not-an-icon.png"


@pytest.mark.parametrize(
    "icon,base,expected",
    [
        (F.RING_ICON, "Two-Stone Ring", "accessory.ring"),
        (F.BOOT_ICON, "Two-Toned Boots", "armour.boots"),
        (F.WAND_ICON, "Profane Wand", "weapon.wand"),
        (F.HELM_ICON, "Hubris Circlet", "armour.helmet"),
        ("", "Leather Belt", "accessory.belt"),  # falls back to the name
        ("", "Opal Wand", "weapon.wand"),
    ],
)
def test_classify(icon, base, expected):
    assert classify(icon, base) == expected


def test_from_stash_json_populates_item(index):
    it = from_stash_json(F.TAILWIND_BOOTS, index, tab_name="dump1")
    assert it.category == "armour.boots"
    assert it.ilvl == 86
    assert it.rarity == "rare"
    assert it.influences == ["hunter"]
    assert it.tab_name == "dump1"
    assert any("Tailwind" in m.text for m in it.mods)


def test_every_fixture_mod_resolves_to_a_stat(index):
    """A mod we cannot map is a mod we cannot search on - catch it early."""
    unresolved = []
    for raw in F.SHOULD_FLAG + F.SHOULD_REJECT:
        it = from_stash_json(raw, index)
        unresolved += [m.text for m in it.mods if not m.stat_id]
    assert unresolved == []


def test_position_is_one_indexed(index):
    it = from_stash_json(F.item(x=3, y=7), index)
    assert it.position == "col 4, row 8"


def test_mod_domains_are_tagged(index):
    it = from_stash_json(F.FRACTURED_RING, index)
    domains = {m.domain for m in it.mods}
    assert "fractured" in domains
    assert it.fractured is True


def test_string_and_object_mods_both_parse(index):
    """The API returns mods as bare strings or {description, flags} objects."""
    as_string = from_stash_json(F.item(explicitMods=["+97 to maximum Life"]), index)
    as_object = from_stash_json(
        F.item(explicitMods=[{"description": "+97 to maximum Life", "flags": {}}]), index
    )
    assert as_string.mods[0].text == as_object.mods[0].text
    assert as_string.mods[0].stat_id == as_object.mods[0].stat_id


def test_crafted_flag_on_object_mod_sets_domain(index):
    it = from_stash_json(
        F.item(explicitMods=[{"description": "+97 to maximum Life", "flags": {"crafted": True}}]),
        index,
    )
    assert it.mods[0].domain == "crafted"


def test_sockets_and_links(index):
    it = from_stash_json(
        F.item(
            sockets=[
                {"group": 0}, {"group": 0}, {"group": 0},
                {"group": 0}, {"group": 0}, {"group": 1},
            ]
        ),
        index,
    )
    assert it.sockets == 6
    assert it.links == 5


# -- wording differences between the game and the trade site ----------------


def test_local_defence_mods_resolve(index):
    """Armour/ES/evasion % mods carry a '(Local)' suffix on trade only."""
    for text in [
        "42% increased Armour and Energy Shield",
        "20% increased Evasion and Energy Shield",
        "22% increased Energy Shield",
        "62% increased Armour and Evasion",
    ]:
        sid, sign = index.resolve(text)
        assert sid is not None, text
        assert sign == 1


def test_reduced_resolves_to_increased_and_negates(index):
    """The game writes 'reduced'; trade stores only 'increased'."""
    sid, sign = index.resolve("32% reduced Attribute Requirements")
    assert sid == index.lookup("32% increased Attribute Requirements")
    assert sign == -1


def test_reduced_duration_on_you_resolves(index):
    sid, sign = index.resolve("33% reduced Poison Duration on you")
    assert sid is not None and sign == -1


def test_implied_count_and_plural_resolve(index):
    """'an additional Curse' is trade's '# additional Curses'."""
    sid, _ = index.resolve("You can apply an additional Curse")
    assert sid is not None


def test_negated_mod_value_is_stored_in_trade_frame(index):
    """A 'reduced' roll must end up negative, or every threshold inverts."""
    it = from_stash_json(F.item(explicitMods=["32% reduced Attribute Requirements"]), index)
    mod = it.mods[0]
    assert mod.stat_id is not None
    assert mod.value == -32


def test_ordinary_increased_mods_keep_their_sign(index):
    it = from_stash_json(F.item(explicitMods=["35% increased Movement Speed"]), index)
    assert it.mods[0].value == 35


def test_local_and_global_energy_shield_are_different_stats(index):
    """'+# to maximum ES' (global) must not collapse into '#% increased ES (Local)'."""
    flat = index.lookup("+23 to maximum Energy Shield")
    local = index.lookup("22% increased Energy Shield")
    assert flat != local


def test_influence_read_from_object_form(index):
    it = from_stash_json(F.item(influences={"hunter": True}), index)
    assert it.influences == ["hunter"]


def test_influence_read_from_legacy_boolean_form(index):
    """Responses also duplicate influence as top-level booleans."""
    it = from_stash_json(F.item(shaper=True, elder=True), index)
    assert it.influences == ["elder", "shaper"]


def test_influence_forms_are_merged_not_doubled(index):
    it = from_stash_json(F.item(influences={"shaper": True}, shaper=True), index)
    assert it.influences == ["shaper"]
