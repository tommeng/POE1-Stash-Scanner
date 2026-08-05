"""Pseudo aggregation, triage rules, and calibration against known items."""

import json
from pathlib import Path

import fixtures as F
import pytest

from poescan.items import from_stash_json
from poescan.pseudo import compute
from poescan.triage import Ruleset, assess, notable_mods

REAL_ITEMS = Path(__file__).parent / "data" / "real_rings.json"


@pytest.fixture(scope="session")
def ruleset():
    return Ruleset.load()


# -- pseudo -----------------------------------------------------------------


def test_flat_life_sums(index):
    it = from_stash_json(F.item(explicitMods=["+97 to maximum Life"]), index)
    assert compute(it).life == 97


def test_strength_contributes_half_life(index):
    it = from_stash_json(
        F.item(explicitMods=["+100 to maximum Life", "+40 to Strength"]), index
    )
    p = compute(it)
    assert p.life == 100
    assert p.effective_life == 120  # 40 str -> 20 life


def test_all_elemental_res_counts_three_times(index):
    it = from_stash_json(F.item(explicitMods=["+16% to all Elemental Resistances"]), index)
    p = compute(it)
    assert p.elemental_resistance == 48
    assert p.count_elemental_resistances == 3


def test_dual_res_counts_twice(index):
    it = from_stash_json(F.item(explicitMods=["+15% to Fire and Cold Resistances"]), index)
    p = compute(it)
    assert p.elemental_resistance == 30
    assert p.resistances["Fire"] == 15
    assert p.resistances["Cold"] == 15
    assert "Lightning" not in p.resistances


def test_chaos_res_excluded_from_elemental(index):
    it = from_stash_json(
        F.item(explicitMods=["+31% to Chaos Resistance", "+40% to Fire Resistance"]), index
    )
    p = compute(it)
    assert p.elemental_resistance == 40
    assert p.total_resistance == 71
    assert p.chaos_resistance == 31


def test_all_attributes_counts_each(index):
    it = from_stash_json(F.item(explicitMods=["+10 to all Attributes"]), index)
    p = compute(it)
    assert p.total_attributes == 30
    assert p.effective_life == 5  # 10 strength -> 5 life


def test_conditional_res_mods_are_ignored(index):
    """'+X% to Fire Resistance while on Low Life' is not real resistance."""
    it = from_stash_json(
        F.item(explicitMods=["+30% to Fire Resistance while on Low Life"]), index
    )
    assert compute(it).elemental_resistance == 0


def test_implicits_count_toward_totals(index):
    it = from_stash_json(
        F.item(explicitMods=["+40% to Fire Resistance"],
               implicitMods=["+15% to Fire and Cold Resistances"]),
        index,
    )
    assert compute(it).elemental_resistance == 70


def test_pseudo_filters_shape(index):
    it = from_stash_json(F.CHAOS_RES_AMULET, index)
    filters = compute(it).as_filters()
    assert filters["pseudo.pseudo_total_life"] == 96
    assert filters["pseudo.pseudo_total_elemental_resistance"] == 113


# -- triage -----------------------------------------------------------------


@pytest.mark.parametrize("raw", F.SHOULD_FLAG, ids=lambda r: r["id"])
def test_valuable_items_are_promoted(raw, index, ruleset):
    v = assess(from_stash_json(raw, index), ruleset)
    assert v.promoted, f"score {v.score} < {ruleset.promote_score}; hits={[h.rule_id for h in v.hits]}"


def _score(raw, index, ruleset):
    return assess(from_stash_json(raw, index), ruleset).score


# Triage is now a coarse filter, not the decider - the market ranks. So the
# invariant that matters is ordering: ordinary items must score strictly below
# items carrying a build-defining mod, whatever the threshold happens to be.
@pytest.mark.parametrize("raw", [F.JUNK_RING, F.LOW_ILVL_RING, F.UNIDENTIFIED],
                         ids=lambda r: r["id"])
def test_worthless_items_are_vetoed_outright(raw, index, ruleset):
    v = assess(from_stash_json(raw, index), ruleset)
    assert v.rejected is not None


def test_ordinary_items_score_below_build_defining_ones(index, ruleset):
    ordinary = max(_score(r, index, ruleset) for r in [F.ORDINARY_RING, F.MID_ATTACK_RING])
    for raw in [F.TAILWIND_BOOTS, F.PLUS_ONE_WAND, F.MINUS_RES_HELM]:
        assert _score(raw, index, ruleset) > ordinary


def test_defence_alone_scores_below_a_rare_mod(index, ruleset):
    """A perfectly rolled but purely defensive ring is still a 1c item."""
    perfect_defence = F.item(
        ilvl=86,
        explicitMods=[
            "+120 to maximum Life",
            "+48% to Fire Resistance",
            "+48% to Cold Resistance",
            "+45% to Lightning Resistance",
        ],
    )
    assert _score(perfect_defence, index, ruleset) < _score(F.MANA_COST_RING, index, ruleset)


def test_negative_value_mods_compare_on_magnitude(index, ruleset):
    """-7 to mana cost must satisfy `min: 4`, not fail it for being negative."""
    v = assess(from_stash_json(F.MANA_COST_RING, index), ruleset)
    assert any(h.rule_id == "minus-mana-cost" for h in v.hits)


def test_template_form_regexes_match_rolled_text(index, ruleset):
    """Rules written as '#% increased ...' must match a real '94% increased ...'."""
    v = assess(from_stash_json(F.PLUS_ONE_WAND, index), ruleset)
    ids = {h.rule_id for h in v.hits}
    assert "caster-gem-level" in ids
    assert "caster-spell-damage" in ids


def test_helm_minus_res_rule_fires(index, ruleset):
    v = assess(from_stash_json(F.MINUS_RES_HELM, index), ruleset)
    assert "helm-enemy-res" in {h.rule_id for h in v.hits}


def test_unidentified_is_rejected(index, ruleset):
    v = assess(from_stash_json(F.UNIDENTIFIED, index), ruleset)
    assert v.rejected == "unidentified"


def test_non_rare_is_rejected(index, ruleset):
    v = assess(from_stash_json(F.item(rarity="Magic"), index), ruleset)
    assert v.rejected == "not rare"


def test_notable_mods_prefers_high_scoring_rules(index, ruleset):
    it = from_stash_json(F.PLUS_ONE_WAND, index)
    mods = notable_mods(assess(it, ruleset), limit=2)
    assert any("Spell Skill Gems" in m.text for m in mods)
    assert all(m.stat_id for m in mods)


def test_hits_record_the_mods_that_matched(index, ruleset):
    v = assess(from_stash_json(F.TAILWIND_BOOTS, index), ruleset)
    tailwind = next(h for h in v.hits if h.rule_id == "boots-tailwind")
    assert any("Tailwind" in m.text for m in tailwind.mods)


# -- calibration against real listings --------------------------------------


@pytest.mark.skipif(not REAL_ITEMS.exists(), reason="real listing sample not present")
def test_real_one_chaos_rings_score_below_valuable_items(index, ruleset):
    """Ten rings scraped from live trade, all listed at 1 chaos.

    The market now does the ranking, so the guarantee is ordering rather than
    exclusion: none of these may out-score a genuinely valuable item.
    """
    rows = json.loads(REAL_ITEMS.read_text())
    worst_valuable = min(
        _score(r, index, ruleset)
        for r in [F.TAILWIND_BOOTS, F.PLUS_ONE_WAND, F.MINUS_RES_HELM]
    )
    offenders = [
        (from_stash_json(r, index).label, assess(from_stash_json(r, index), ruleset).score)
        for r in rows
        if assess(from_stash_json(r, index), ruleset).score >= worst_valuable
    ]
    assert offenders == [], f"junk rings out-scored real value: {offenders}"
