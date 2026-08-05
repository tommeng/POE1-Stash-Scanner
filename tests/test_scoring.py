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


# -- life bands -------------------------------------------------------------
#
# The two life rules are disjoint bands rather than a base score plus a bonus.
# Nested additive rules cannot be told apart by `analyse`: the narrower one can
# never fire alone, so it reports the same population and the same median as
# the wider one no matter what it is worth. These tests pin the bands apart.


def _life_item(effective_life: int):
    """A body armour whose only notable property is its life roll."""
    return F.item(baseType="Astral Plate", explicitMods=[f"+{effective_life} to maximum Life"])


def _rules_for(raw, index, ruleset) -> set:
    return {h.rule_id for h in assess(from_stash_json(raw, index), ruleset).hits}


@pytest.mark.parametrize("life", [90, 100, 114])
def test_mid_life_roll_scores_the_lower_band_only(life, index, ruleset):
    assert _rules_for(_life_item(life), index, ruleset) == {"high-life"}


@pytest.mark.parametrize("life", [115, 130, 200])
def test_top_life_roll_scores_the_upper_band_only(life, index, ruleset):
    """The upper band replaces the lower one; it is not a surcharge on top."""
    assert _rules_for(_life_item(life), index, ruleset) == {"very-high-life"}


def test_life_bands_never_both_fire(index, ruleset):
    """Regression: the bands were once nested, double-scoring every top roll."""
    for life in range(85, 205, 5):
        fired = _rules_for(_life_item(life), index, ruleset) & {"high-life", "very-high-life"}
        assert len(fired) <= 1, f"{life} life fired {fired}"


def test_a_top_life_roll_alone_still_earns_a_market_check(index, ruleset):
    """Life alone is usually a 5c item, but the tail is not: of the 20 priced
    items promoted by life alone, four were worth 25c-75c. Triage is a filter,
    so this band is deliberately scored above promote_score."""
    v = assess(from_stash_json(_life_item(130), index), ruleset)
    assert v.promoted


def test_a_merely_high_life_roll_does_not_earn_a_market_check(index, ruleset):
    """The lower band is a co-factor - it needs a second signal to promote."""
    v = assess(from_stash_json(_life_item(100), index), ruleset)
    assert not v.promoted


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


# -- base type conditions ---------------------------------------------------


def _base_rules(*conds) -> Ruleset:
    """A one-rule ruleset scoring only on the given conditions."""
    return Ruleset(
        {
            "settings": {"promote_score": 1, "min_ilvl": 0},
            "rules": [{"id": "base-test", "score": 5, "all": list(conds)}],
        }
    )


def _fires(raw, index, rs) -> bool:
    return "base-test" in {h.rule_id for h in assess(from_stash_json(raw, index), rs).hits}


def test_none_conditions_suppress_an_otherwise_matching_rule(index):
    """`none` is how one rule is carved out of a wider one, as the life bands do."""
    rs = Ruleset(
        {
            "settings": {"promote_score": 1, "min_ilvl": 0},
            "rules": [
                {
                    "id": "base-test",
                    "score": 5,
                    "all": [{"pseudo": "life", "min": 90}],
                    "none": [{"pseudo": "life", "min": 115}],
                }
            ],
        }
    )
    assert _fires(F.item(explicitMods=["+100 to maximum Life"]), index, rs)
    assert not _fires(F.item(explicitMods=["+130 to maximum Life"]), index, rs)
    assert not _fires(F.item(explicitMods=["+50 to maximum Life"]), index, rs)


def test_base_matches_exact_name(index):
    assert _fires(F.item(baseType="Two-Stone Ring"), index, _base_rules({"base": "Two-Stone Ring"}))


def test_base_is_case_insensitive(index):
    assert _fires(F.item(baseType="Two-Stone Ring"), index, _base_rules({"base": "two-stone ring"}))


def test_base_does_not_match_a_different_base(index):
    assert not _fires(F.item(baseType="Two-Stone Ring"), index, _base_rules({"base": "Opal Ring"}))


def test_base_any_matches_any_listed(index):
    rs = _base_rules({"base_any": ["Opal Ring", "Two-Stone Ring"]})
    assert _fires(F.item(baseType="Two-Stone Ring"), index, rs)


def test_base_is_not_a_substring_match(index):
    """Substring matching would conflate 'Ruby Ring' with 'Ruby Flask'."""
    assert not _fires(F.item(baseType="Ruby Flask"), index, _base_rules({"base": "Ruby Ring"}))


def test_base_matches_covers_families(index):
    rs = _base_rules({"base_matches": "Two-Stone|Opal"})
    assert _fires(F.item(baseType="Two-Stone Ring"), index, rs)
    assert not _fires(F.item(baseType="Iron Ring"), index, rs)


def test_base_combines_with_ilvl(index):
    """Base value is conditional, so a base rule is expected to be qualified."""
    rs = _base_rules({"base": "Two-Stone Ring"}, {"ilvl": {"min": 84}})
    assert _fires(F.item(baseType="Two-Stone Ring", ilvl=86), index, rs)
    assert not _fires(F.item(baseType="Two-Stone Ring", ilvl=80), index, rs)


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
