"""Pseudo aggregation, triage rules, and calibration against known items."""

import json
from pathlib import Path

import fixtures as F
import pytest
import yaml

from poescan.items import from_stash_json
from poescan.pseudo import compute
from poescan.triage import (
    DEFAULT_RULES,
    Ruleset,
    RulesetError,
    ambiguous_conditions,
    assess,
    notable_mods,
)

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


# -- resistance bands -------------------------------------------------------


def _res_item(fire: int, cold: int, lightning: int, chaos: int = 0):
    """A ring whose only notable property is its resistance spread."""
    mods = [
        f"+{fire}% to Fire Resistance",
        f"+{cold}% to Cold Resistance",
        f"+{lightning}% to Lightning Resistance",
    ]
    if chaos:
        mods.append(f"+{chaos}% to Chaos Resistance")
    return F.item(explicitMods=mods)


@pytest.mark.parametrize("spread", [(44, 44, 44), (45, 45, 45), (48, 48, 48)])
def test_high_total_res_band_excludes_the_top_band(spread, index, ruleset):
    fired = _rules_for(_res_item(*spread), index, ruleset)
    assert "huge-total-res" not in fired
    assert "high-total-res" in fired


@pytest.mark.parametrize("spread", [(54, 54, 54), (60, 60, 60)])
def test_top_res_band_replaces_the_lower_one(spread, index, ruleset):
    """Not a surcharge: 160+ scores the upper band alone, same 12 as before."""
    fired = _rules_for(_res_item(*spread), index, ruleset)
    assert "huge-total-res" in fired
    assert "high-total-res" not in fired


def test_res_bands_never_both_fire(index, ruleset):
    """Regression: nested, these double-scored every 160+ roll."""
    for each in range(40, 71, 2):
        fired = _rules_for(_res_item(each, each, each), index, ruleset) & {
            "high-total-res", "huge-total-res"
        }
        assert len(fired) <= 1, f"{each * 3} total res fired {fired}"


def test_splitting_the_res_bands_preserved_every_total(index, ruleset):
    """The split must not move any score: 6 below 160, 12 at or above."""
    for each in range(40, 71, 2):
        v = assess(from_stash_json(_res_item(each, each, each), index), ruleset)
        band = {h.rule_id: h.score for h in v.hits}
        total = band.get("high-total-res", 0) + band.get("huge-total-res", 0)
        expected = 12 if each * 3 >= 160 else 6 if each * 3 >= 130 else 0
        assert total == expected, f"{each * 3} total res scored {total}, expected {expected}"


def test_triple_res_still_stacks_with_a_total_band(index, ruleset):
    """It keys off the count of resistances, not the total, so it is a
    genuinely separate signal and is deliberately left additive."""
    fired = _rules_for(_res_item(45, 45, 45), index, ruleset)
    assert {"triple-res", "high-total-res"} <= fired


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


# -- one condition names one selector ---------------------------------------
#
# `{base: Opal Ring, ilvl: {min: 84}}` reads as a conjunction and is not one:
# only one selector was evaluated and the other was discarded, so that rule
# fired on an ilvl 86 Iron Ring. The form is now refused when the ruleset loads,
# because `validate-rules` is a command the user has to remember to run.


AMBIGUOUS = "ambiguous"


def _load(*conds, section: str = "rules", group: str = "all") -> Ruleset:
    rule = {"id": AMBIGUOUS, "score": 5, group: list(conds)}
    return Ruleset({"settings": {"promote_score": 1, "min_ilvl": 0}, section: [rule]})


def _fired(raw, index, rs) -> bool:
    return AMBIGUOUS in {h.rule_id for h in assess(from_stash_json(raw, index), rs).hits}


@pytest.mark.parametrize(
    "cond",
    [
        pytest.param({"base": "Opal Ring", "ilvl": {"min": 84}}, id="base-and-ilvl"),
        pytest.param({"ilvl": {"min": 84}, "links": {"min": 5}}, id="two-scalars"),
        pytest.param({"pseudo": "life", "min": 90, "flag": "influenced"}, id="pseudo-and-flag"),
        pytest.param({"mod": "+# to maximum Life", "mod_matches": "Life"}, id="mod-and-matches"),
        pytest.param({"base": "Opal Ring", "base_matches": "Ring"}, id="base-and-matches"),
        pytest.param({"category": "accessory.ring", "flag": "corrupted"}, id="category-and-flag"),
    ],
)
def test_a_condition_naming_two_selectors_is_refused_at_load(cond):
    with pytest.raises(RulesetError):
        _load(cond)


def test_the_refusal_names_the_rule_and_the_conflicting_keys():
    """The message has to be enough to find the line in the YAML."""
    with pytest.raises(RulesetError) as raised:
        _load({"base": "Opal Ring", "ilvl": {"min": 84}})
    message = str(raised.value)
    assert AMBIGUOUS in message
    assert "base" in message and "ilvl" in message


@pytest.mark.parametrize("group", ["all", "any", "none"])
def test_every_condition_group_is_checked(group):
    with pytest.raises(RulesetError):
        _load({"base": "Opal Ring", "ilvl": {"min": 84}}, group=group)


def test_veto_rules_are_checked_too():
    """A veto that half-evaluates discards items, which is the costlier mistake."""
    with pytest.raises(RulesetError):
        _load({"base": "Opal Ring", "ilvl": {"min": 84}}, section="veto")


def test_a_selector_with_min_and_max_is_not_ambiguous(index):
    """`{pseudo: attributes, min: 70}` is the normal, correct form."""
    rs = _load({"pseudo": "life", "min": 90, "max": 115})
    assert _fired(F.item(explicitMods=["+100 to maximum Life"]), index, rs)
    assert not _fired(F.item(explicitMods=["+130 to maximum Life"]), index, rs)


def test_aliases_of_one_selector_may_share_a_condition(index):
    """`mod` and `mod_any` are unioned by the evaluator, so they do not conflict."""
    rs = _load({"mod": "+# to maximum Life", "mod_any": ["+# to Strength"], "min": 20})
    assert _fired(F.item(explicitMods=["+25 to Strength"]), index, rs)
    assert _fired(F.item(explicitMods=["+99 to maximum Life"]), index, rs)
    assert not _fired(F.item(explicitMods=["+5 to Strength"]), index, rs)


def test_the_shipped_ruleset_names_one_selector_per_condition():
    """Nothing in `default.yaml` used the ambiguous form, and nothing may start."""
    assert ambiguous_conditions(yaml.safe_load(DEFAULT_RULES.read_text())) == []


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


def test_a_known_multi_divine_amulet_is_still_promoted(index, ruleset):
    """The guard against the ruleset drifting *tight*.

    The ten 1c rings above guard the loose direction. This is the other side: a
    real Turquoise Amulet whose comparables on the trade site ran from 20 chaos
    to 10 divine, recorded in `TrainingData/Hypnotic Beads, Turquoise Amulet/`.

    When it was recorded it scored exactly promote_score - a margin of zero, on
    `attribute-stacking` alone (76 total attributes against a min of 70). One
    point off that roll, or one point onto that threshold, drops it. So if a
    rule edit makes this fail, the edit has introduced a false negative on a
    multi-divine item, which is the failure mode that costs the user money.

    Only promotion is asserted. The score is not: it is an observation about a
    hand-authored prior, not a fact the ruleset is obliged to reproduce.
    """
    v = assess(from_stash_json(F.HYPNOTIC_BEADS_AMULET, index), ruleset)
    assert v.promoted, (
        f"scored {v.score} against promote_score {ruleset.promote_score} "
        f"(10 vs 10 when recorded); hits={[h.rule_id for h in v.hits]}"
    )
