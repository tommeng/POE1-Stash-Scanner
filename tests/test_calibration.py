"""The arithmetic `analyse` renders, and the labelling that keeps it honest.

This is the code the ruleset's scores are meant to be corrected by, so it is
tested on the two ways it has already been observed to mislead: reporting a
median where the decision depends on the tail, and pooling rule labels from
rulesets that no longer exist.
"""

import pytest
import yaml

from poescan import calibration as cal
from poescan.triage import DEFAULT_RULES, Ruleset, fingerprint


def obs(median, score=10.0, rules=(), base="Opal Ring", ruleset="abc123", total=10):
    """One row shaped like `Cache.observations` returns."""
    return {
        "median": median,
        "cheapest": median,
        "total": total,
        "triage_score": score,
        "rules_hit": list(rules),
        "base_type": base,
        "ruleset": ruleset,
    }


# -- the tail is the measurement --------------------------------------------

# The seven `fractured` observations CLAUDE.md records as "blocked on
# measurement". They are not short of data; they were read with a statistic
# that cannot see what the decision turns on.
FRACTURED = [1.0, 1.0, 1.0, 1.0, 4.5, 100.0, 202.5]


def test_summarise_reports_the_tail_beside_the_median():
    g = cal.summarise("fractured", FRACTURED, threshold=50.0)
    assert g.n == 7
    assert g.median == 1.0, "the median says delete the rule"
    assert g.best == 202.5, "the tail says the opposite"
    assert g.tail == 2


def test_a_rule_that_catches_value_outranks_one_with_a_better_median():
    """Triage is a filter, judged on what it catches, not on its typical item.

    `steady` looks better in every median-shaped report and is worth less: it
    has never caught anything above the threshold, while `spiky` caught two.
    """
    rows = [obs(p, rules=["spiky"]) for p in FRACTURED]
    rows += [obs(8.0, rules=["steady"]) for _ in range(7)]

    ranked = cal.by_rule(rows, threshold=50.0)
    assert [g.key for g in ranked] == ["spiky", "steady"]
    assert ranked[0].median < ranked[1].median


def test_min_samples_hides_thin_rows():
    rows = [obs(500.0, rules=["anecdote"]), *[obs(5.0, rules=["real"]) for _ in range(3)]]
    assert [g.key for g in cal.by_rule(rows, min_samples=3)] == ["real"]


def test_unpriced_observations_are_dropped_not_counted_as_zero():
    """A check that matched nothing is a fact about scarcity, not a price."""
    rows = [obs(40.0), obs(None), obs(60.0)]
    assert len(cal.priced(rows)) == 2
    assert cal.baseline(cal.priced(rows)) == 50.0


# -- score bands ------------------------------------------------------------


def test_score_bands_stay_in_score_order():
    """The question is whether price rises with score, which only reads in order."""
    rows = [obs(5.0, score=12), obs(90.0, score=35), obs(1.0, score=17), obs(15.0, score=25)]
    assert [g.key for g in cal.by_score_band(rows)] == ["10-14", "15-19", "20-29", "30+"]


def test_tail_below_measures_what_raising_promote_score_would_discard():
    """The 820c item in the author's cache scored 10 against a promote_score of 10.

    Three separate items have now cleared the bar by a margin of zero, so the
    cost of demanding a higher score is a number worth printing rather than a
    thing to be reasoned about.
    """
    rows = [
        obs(820.0, score=10),  # most valuable item in the set, lowest band
        obs(718.0, score=12),
        obs(615.0, score=24),
        obs(2.0, score=31),  # top-scoring item, worth nothing
    ]
    missed, valuable = cal.tail_below(rows, score=15, threshold=50.0)
    assert (missed, valuable) == (2, 3)


def test_never_fired_lists_unmeasured_rules():
    ruleset = Ruleset.load()
    fired = cal.never_fired([obs(5.0, rules=["influenced"])], ruleset)
    assert "influenced" not in fired
    assert "fractured" in fired


# -- ruleset vocabulary -----------------------------------------------------


def test_observations_from_another_ruleset_are_separable():
    rows = [obs(5.0, ruleset="aaa"), obs(6.0, ruleset="bbb"), obs(7.0, ruleset="aaa")]
    assert len(cal.matching(rows, "aaa")) == 2
    assert len(cal.stale(rows, "aaa")) == 1
    assert cal.vocabularies(rows) == [("aaa", 2), ("bbb", 1)]


def test_rows_written_before_fingerprinting_count_as_stale():
    """An absent label is an unknown ruleset, not the current one."""
    row = obs(5.0)
    del row["ruleset"]
    assert cal.stale([row], "aaa") == [row]
    assert cal.vocabularies([row]) == [(cal.UNLABELLED, 1)]


# -- the fingerprint itself -------------------------------------------------


@pytest.fixture(scope="module")
def default_data():
    return yaml.safe_load(DEFAULT_RULES.read_text())


def test_retuning_a_score_changes_the_fingerprint(default_data):
    before = fingerprint(default_data)
    edited = yaml.safe_load(DEFAULT_RULES.read_text())
    edited["rules"][0]["score"] = float(edited["rules"][0]["score"]) + 1
    assert fingerprint(edited) != before


def test_changing_a_threshold_changes_the_fingerprint(default_data):
    edited = yaml.safe_load(DEFAULT_RULES.read_text())
    edited["settings"]["promote_score"] = 30
    assert fingerprint(edited) != fingerprint(default_data)


def test_the_api_budget_knob_does_not_orphan_observations(default_data):
    """`max_market_checks` caps how many items get priced, not how one is judged.

    Bumping it must not invalidate evidence already collected, or the budget
    would be untunable without paying to re-gather everything.
    """
    edited = yaml.safe_load(DEFAULT_RULES.read_text())
    edited["settings"]["max_market_checks"] = 999
    assert fingerprint(edited) == fingerprint(default_data)


def test_the_fingerprint_is_stable_across_loads():
    assert Ruleset.load().fingerprint == Ruleset.load().fingerprint


def test_splitting_a_rule_into_bands_changes_the_fingerprint():
    """The exact edit that orphaned the author's 135 observations.

    Nested life rules meant an item over 115 hit both `high-life` and
    `very-high-life`; the disjoint bands make that impossible. Observations
    carrying both ids therefore describe a ruleset that no longer exists, and
    27 of them were being reported as evidence about the one that replaced it.
    """
    nested = {
        "rules": [
            {"id": "high-life", "score": 7, "all": [{"pseudo": "life", "min": 90}]},
            {"id": "very-high-life", "score": 5, "all": [{"pseudo": "life", "min": 115}]},
        ]
    }
    split = {
        "rules": [
            {
                "id": "high-life",
                "score": 7,
                "all": [{"pseudo": "life", "min": 90}],
                "none": [{"pseudo": "life", "min": 115}],
            },
            {"id": "very-high-life", "score": 12, "all": [{"pseudo": "life", "min": 115}]},
        ]
    }
    assert fingerprint(nested) != fingerprint(split)
