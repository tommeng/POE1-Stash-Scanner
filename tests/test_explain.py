"""What the ruleset page claims about the ruleset, and whether it is true.

A page that documents the scoring model has exactly one asset: being believed.
Two failures would spend it, and they are what this file is mostly about.

**A false alarm against the shipped ruleset.** The lint here reports conditions
that are dead or wider than they read - forms `validate-rules` structurally
cannot see, because they are built entirely from keys it recognises. If it fires
on a clean ruleset, every real finding it ever reports is noise. That is
`test_the_shipped_ruleset_produces_no_problems_and_no_cautions`, and a failure
there means the lint is wrong, not that the test is too strict.

**A wording that is confidently wrong about a number.** `min: 11` on `Adds # to
# Physical Damage` is a bar on the *average* of the roll, and `abs: true` does
not mean "this mod rolls negative" - it means a +4 roll satisfies a rule written
for -4, which is a real defect in the shipped ruleset that the page must expose
rather than paper over. Each wording trap below is one sentence that would
otherwise read plausibly and be false.

Nothing here uses the `index` fixture: needing trade metadata to document a
ruleset would mean the command had grown a network dependency.
"""

import re
import sqlite3

import pytest

from poescan import calibration as cal, cli, explain, report as report_mod, triage
from poescan.config import Config
from poescan.explain import CAUTION, PROBLEM
from poescan.items import Item, Mod
from poescan.pseudo import compute
from poescan.triage import Ruleset

MODIFIERS = ("min", "max", "abs", "is")


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    """Point every `Cache()` in the CLI at a throwaway file, not the user's.

    Module-wide rather than per-test: `POESCAN_HOME` is read at *import* time, so
    the module attribute is what has to move - patching the environment variable
    here would do nothing at all - and one test forgetting this rewrites the
    developer's own cache.
    """
    import poescan.cache as cache_mod

    monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "cache.sqlite")
    monkeypatch.setattr(
        cli.Config, "load", classmethod(lambda c: Config(client_id="x", league="Allflame"))
    )


@pytest.fixture(scope="module")
def shipped():
    """The real ruleset, described. Reads the YAML and nothing else."""
    return explain.describe(Ruleset.load())


def card(doc, rule_id: str) -> explain.RuleCard:
    for c in doc.cards:
        if c.id == rule_id:
            return c
    raise AssertionError(f"no rule '{rule_id}' in the ruleset")


def lines(c: explain.RuleCard) -> list[explain.ConditionLine]:
    return [line for group in c.groups for line in group.lines]


def texts(c: explain.RuleCard) -> str:
    return " | ".join([c.note] + [g.lead for g in c.groups] + [line.text for line in lines(c)])


def one(cond: dict) -> explain.ConditionLine:
    return explain.describe_condition(cond)


def ruleset(rules: list[dict], veto: list[dict] | None = None) -> Ruleset:
    return Ruleset({"settings": {"promote_score": 10}, "rules": rules, "veto": veto or []})


# -- structural guards: the reason this file exists --------------------------


def test_the_shipped_ruleset_produces_no_problems_and_no_cautions(shipped):
    """A red badge on a correct rule is the worst failure available here.

    If this fails, the lint has a false positive. Fix the lint - loosening the
    test converts a documentation page into a source of invented alarms.
    """
    assert shipped.problems == ()
    assert shipped.cautions == ()


def test_every_selector_family_the_evaluator_reads_has_a_rendering():
    """Compared as families, not as a flat key set, so regrouping is caught too.

    `mod_matches` was added to the evaluator and never to `validate-rules`, and
    the eleven highest-scoring rules in the ruleset went unvalidated for months
    as a result. A selector with no rendering here is the same class of bug: it
    would document itself as nothing at all.
    """
    assert set(explain._RENDERERS) == set(triage.SELECTOR_FAMILIES)
    assert set(explain._CONSUMES) == set(triage.SELECTOR_FAMILIES)


def test_every_selector_family_can_be_described_without_raising():
    """`describe_condition` is the one entry point the page calls per condition."""
    for family in triage.SELECTOR_FAMILIES:
        cond = {key: "x" for key in family}
        assert one(cond).text


# -- the ignored-modifier finding, measured against the evaluator ------------


def _item(**kw) -> Item:
    base = {
        "id": "probe",
        "name": "Probe",
        "base_type": "Opal Ring",
        "type_line": "Opal Ring",
        "category": "accessory.ring",
        "ilvl": 84,
        "rarity": "rare",
    }
    base.update(kw)
    return Item(**base)


def _mod(text: str, values: list[float], stat_id: str | None = None) -> Mod:
    return Mod(text=text, domain="explicit", stat_id=stat_id, values=values)


LIFE_ITEM = _item(mods=[_mod("+100 to maximum Life", [100.0])])
MANA_COST = "-# to Total Mana Cost of Skills"
MANA_ITEM = _item(
    mods=[_mod("-7 to Total Mana Cost of Skills", [-7.0], stat_id="explicit.stat_mana")]
)
INFLUENCED = _item(influences=["shaper"])
SOCKETED = _item(
    links=6,
    sockets=6,
    quality=20,
    influences=["shaper", "elder"],
    mods=[_mod("+100 to maximum Life", [100.0])],
)

# One probe per shape the modifier could plausibly matter in: a condition that
# holds (so a bound can break it) and one that does not (so a bound can fix it).
PROBES: dict[frozenset[str], tuple[tuple[dict, Item], ...]] = {
    frozenset({"pseudo"}): (
        ({"pseudo": "life"}, LIFE_ITEM),
        ({"pseudo": "life", "min": 1000}, LIFE_ITEM),
    ),
    frozenset({"mod", "mod_any"}): (
        ({"mod": MANA_COST}, MANA_ITEM),
        ({"mod": MANA_COST, "min": 4}, MANA_ITEM),
    ),
    frozenset({"mod_matches"}): (
        ({"mod_matches": "Total Mana Cost"}, MANA_ITEM),
        ({"mod_matches": "Total Mana Cost", "min": 4}, MANA_ITEM),
    ),
    frozenset({"stat", "stat_any"}): (
        ({"stat": "explicit.stat_mana"}, MANA_ITEM),
        ({"stat": "explicit.stat_mana", "min": 4}, MANA_ITEM),
    ),
    frozenset({"flag"}): (
        ({"flag": "influenced"}, INFLUENCED),
        ({"flag": "corrupted"}, INFLUENCED),
    ),
    frozenset({"base", "base_any"}): (
        ({"base": "Opal Ring"}, SOCKETED),
        ({"base": "Iron Ring"}, SOCKETED),
    ),
    frozenset({"base_matches"}): (
        ({"base_matches": "Ring"}, SOCKETED),
        ({"base_matches": "Belt"}, SOCKETED),
    ),
    frozenset({"category"}): (
        ({"category": ["accessory.ring"]}, SOCKETED),
        ({"category": ["flask"]}, SOCKETED),
    ),
    **{
        frozenset({name}): (
            ({name: {"min": 1}}, SOCKETED),
            ({name: {"min": 1000}}, SOCKETED),
        )
        for name in ("ilvl", "links", "sockets", "quality", "influence_count", "mod_count")
    },
}

CANDIDATE_VALUES = {
    "min": (1000, -1000),
    "max": (1000, -1000),
    "abs": (True, False),
    "is": (True, False),
}


def _changes_the_answer(probes, modifier: str) -> bool:
    for cond, item in probes:
        p = compute(item)
        before = triage.evaluate_condition(cond, item, p)
        for value in CANDIDATE_VALUES[modifier]:
            if triage.evaluate_condition({**cond, modifier: value}, item, p) != before:
                return True
    return False


@pytest.mark.parametrize("family", list(PROBES), ids=lambda f: "+".join(sorted(f)))
def test_the_declared_modifier_consumption_matches_what_the_evaluator_actually_reads(family):
    """`_CONSUMES` is what the ignored-modifier finding is derived from.

    Asserted against the evaluator rather than against the source it was read
    off, so the finding cannot quietly start accusing a modifier that is in fact
    honoured - or stop noticing one that is not. The scalars are the case worth
    watching: their bound is nested under the selector key, so a *top-level*
    `min` beside `ilvl` really does nothing, and `validate-rules` cannot say so
    because `min` is a perfectly good condition key.
    """
    observed = {m for m in MODIFIERS if _changes_the_answer(PROBES[family], m)}
    assert observed == set(explain._CONSUMES[family])


def test_every_family_is_probed():
    """A family with no probe would pass the test above by not being run."""
    assert set(PROBES) == set(triage.SELECTOR_FAMILIES)


# -- wording traps ----------------------------------------------------------


def test_a_nested_scalar_threshold_and_a_bare_number_read_the_same():
    """`_in_range` treats a bare number as ">=", so both spellings are one rule."""
    assert one({"ilvl": {"min": 86}}).text == "item level 86 or more"
    assert one({"ilvl": 86}).text == one({"ilvl": {"min": 86}}).text


def test_a_max_only_threshold_reads_as_an_upper_bound(shipped):
    """Inverting a veto is the most damaging wording bug available here.

    `low-ilvl-jewellery` silently discards every uninfluenced, unfractured ring,
    amulet and belt at ilvl 74 *or below*. Rendered as "74 or more" the page
    would describe a ruleset that rejects exactly the items it keeps.
    """
    veto = card(shipped, "low-ilvl-jewellery")
    assert "item level 74 or less" in texts(veto)
    assert "74 or more" not in texts(veto)


def test_a_min_and_max_pair_states_both_bounds_literally():
    """`max: 159` admits 159.5, so "under 160" would be wrong for a fractional
    roll. The arithmetic-free "but under" wording belongs to `bands`, where the
    ceiling really is the neighbouring rule's `min`."""
    assert one({"ilvl": {"min": 130, "max": 159}}).text == (
        "item level 130 or more, and 159 or less"
    )


def test_a_range_mod_says_its_numbers_are_averaged_and_gives_the_implied_total():
    """`Mod.value` is the mean of a multi-number roll (`items.py`), so `min: 11`
    on `Adds # to # Physical Damage` is a bar on the average - about 22 damage in
    total. Read as a flat bar of 11 it understates the rule by half."""
    text = one({"mod": "Adds # to # Physical Damage to Attacks", "min": 11}).text
    assert "averaging 11 or more" in text
    assert "22 in total" in text


def test_abs_true_says_a_positive_roll_of_the_same_size_also_satisfies_it():
    """The plausible reading - "this mod rolls negative" - is false, and hides a
    real defect: `_best_mod` compares `abs(mod.value)`, and `minus-mana-cost`
    lists the unsigned template too, so a **+4** roll on a strictly bad mod
    scores 26. The page reports what the evaluator does."""
    text = one({"mod_any": [MANA_COST, "# to Total Mana Cost of Skills"], "min": 4, "abs": True}).text
    assert "absolute value" in text
    assert "a positive roll of the same size also satisfies it" in text
    assert "rolls negative" not in text


def test_a_flag_with_is_false_is_worded_as_a_negation():
    assert one({"flag": "influenced"}).text == "has an influence"
    assert one({"flag": "influenced", "is": False}).text == "does not have an influence"
    assert one({"flag": "fractured", "is": False}).text == "is not fractured"


def test_a_multi_template_mod_any_is_not_worded_as_a_list_of_alternatives():
    """`mod_any` tests the single highest-magnitude match against the bound. An
    `any:` block of two conditions means either condition holds on its own - a
    different claim, and the one "any of these" would be read as."""
    text = one({"mod_any": [MANA_COST, "# to Total Mana Cost of Skills"], "min": 4}).text
    assert "whichever of these 2 mods rolls highest" in text
    assert "any of these" not in text


def test_a_regex_condition_is_paraphrased_and_still_shows_the_exact_pattern():
    """The paraphrase is for reading; the literal is what you copy into the YAML."""
    line = one({"mod_matches": r"Critical Strike (Multiplier|Chance)", "min": 20})
    assert 'has a mod matching "Critical Strike Multiplier or Chance"' in line.text
    assert line.detail == (r"Critical Strike (Multiplier|Chance)",)
    assert line.is_pattern


@pytest.mark.parametrize(
    "pattern,expected",
    [
        (r"\+# to Level of all .*Spell Skill Gems", "+# to Level of all … Spell Skill Gems"),
        (r"\+# to Level of all .*(Skill Gems)", "+# to Level of all … Skill Gems"),
        (r"Nearby Enemies have -#% to .* Resistance", "Nearby Enemies have -#% to … Resistance"),
        (r"^9\d% increased Spell Damage$", "9#% increased Spell Damage"),
        (r"Tailwind", "Tailwind"),
    ],
)
def test_the_patterns_the_shipped_ruleset_uses_paraphrase_into_readable_mods(pattern, expected):
    assert explain.paraphrase_pattern(pattern) == expected


def test_an_unparaphrasable_pattern_falls_through_verbatim_rather_than_raising():
    """A pattern that does not even compile still has to render: this is the
    command that shows a broken rule *visibly*, so it cannot be the one that
    dies on it."""
    assert explain.paraphrase_pattern("increased (") == "increased ("
    assert explain.paraphrase_pattern(r"(?<=Socketed)\s+Gems") == r"(?<=Socketed)\s+Gems"
    broken = one({"mod_matches": "increased ("})
    assert [f.level for f in broken.findings] == [PROBLEM]
    assert "does not compile" in broken.findings[0].text


# -- the lint ---------------------------------------------------------------


def test_a_condition_key_the_evaluator_does_not_read_is_reported_not_dropped():
    """`{psuedo: life}` matches nothing, silently, forever. It is also invisible
    to a page that renders only the keys it recognises."""
    line = one({"psuedo": "life", "min": 90})
    assert [f.level for f in line.findings] == [PROBLEM, PROBLEM]
    assert any("never matches any item" in f.text for f in line.findings)
    assert any("`psuedo`" in f.text for f in line.findings)

    doc = explain.describe(ruleset([{"id": "typo", "score": 5, "all": [{"psuedo": "life"}]}]))
    assert {rid for rid, _ in doc.problems} == {"typo"}, "the badge names the rule"
    assert doc.cautions == ()


@pytest.mark.parametrize(
    "cond,why",
    [
        ({"pseudo": "life"}, "an unbounded pseudo total: `_in_range` falls through to True"),
        ({"ilvl": {}}, "a scalar with an empty bound mapping"),
        ({"flag": "influencd", "is": False}, "an unknown flag reads as false, so `is: false` holds"),
    ],
)
def test_the_always_true_condition_forms_are_flagged(cond, why):
    """Each of these adds its rule's whole score to every item scored, which
    reads on the page as a rule that does something. None is a key-set error, so
    `validate-rules` cannot see any of them."""
    line = one(cond)
    assert [f.level for f in line.findings] == [CAUTION], why
    assert "every item" in line.findings[0].text


def test_a_quoted_number_threshold_is_flagged_as_never_satisfiable():
    """`{ilvl: "86"}` is the easiest YAML typo there is: `_in_range` gets a
    string, is not given a dict, and returns False for every item ever scored."""
    item = _item(ilvl=100)
    assert triage.evaluate_condition({"ilvl": "86"}, item, compute(item)) is False

    line = one({"ilvl": "86"})
    assert [f.level for f in line.findings] == [PROBLEM]
    assert "never true for any item" in line.findings[0].text


def test_a_condition_with_two_findings_reports_both():
    """`{ilvl: {}, min: 86}` is always true *and* ignores the bound its author
    clearly meant. Pinned because a `problem` string beside a `caution` string
    could only ever show one of them, and there is no way to rank these two."""
    findings = one({"ilvl": {}, "min": 86}).findings
    assert len(findings) == 2
    assert {f.level for f in findings} == {CAUTION}
    assert any("every item" in f.text for f in findings)
    assert any("`min` is not read" in f.text for f in findings)


def test_a_condition_naming_two_selectors_is_reported_without_naming_a_winner():
    """`conflicting_selectors` returns its keys sorted, so it cannot say which
    one wins, and duplicating the evaluator's if-chain to find out would be a
    second copy of the scoring order."""
    findings = one({"base": "Opal Ring", "ilvl": {"min": 84}}).findings
    assert PROBLEM in [f.level for f in findings]
    assert any("more than one selector" in f.text for f in findings)


# -- structure: bands, groups and slots -------------------------------------


def test_the_two_life_rules_are_presented_as_one_pair_of_bands(shipped):
    """The `none` block exists to make the bands disjoint, not to penalise a
    good roll, and the ceiling belongs in the headline of the lower band."""
    low, high = card(shipped, "high-life"), card(shipped, "very-high-life")

    assert low.band.partner_id == "very-high-life" and low.band.lower
    assert high.band.partner_id == "high-life" and not high.band.lower
    assert "effective life 90 or more, but under 115" in texts(low)
    assert [g.kind for g in low.groups] == ["all"], "the band note replaces the `none` block"


def test_only_the_rules_written_as_bands_are_paired(shipped):
    """Pairing is exact dict equality, which is the whole safety of it: two
    candidates in the ruleset, two true positives, and no way to invent a third
    from rules that merely look similar. Detecting rules that *stack* is
    deliberately not attempted - the obvious heuristic would claim a +2 socketed
    gem roll "fires both, scoring 46", where the higher threshold in fact
    carries the lower score."""
    assert set(explain.bands(shipped.rules)) == {
        "high-life",
        "very-high-life",
        "high-total-res",
        "huge-total-res",
    }


def test_a_none_condition_that_is_not_a_band_boundary_is_worded_as_an_exclusion(shipped):
    """"Must not have 115 life" reads as a penalty for rolling well, which is
    the opposite of what every `none` block in this ruleset means."""
    doc = explain.describe(
        ruleset(
            [
                {
                    "id": "clean-life",
                    "score": 5,
                    "all": [{"pseudo": "life", "min": 90}],
                    "none": [{"flag": "corrupted"}],
                }
            ]
        )
    )
    excluded = card(doc, "clean-life")
    assert excluded.band is None
    assert [g.lead for g in excluded.groups] == ["Needs all of", "Excluded when"]
    assert "Must not have" not in " ".join(texts(c) for c in shipped.cards)


def test_a_rule_with_both_all_and_any_renders_both_groups(shipped):
    """`ilvl-86-craft-base` is the only rule combining the two sections, so this
    clause combination is otherwise untested: ilvl 86 *and* one of three flags.
    """
    craft = card(shipped, "ilvl-86-craft-base")
    assert [g.kind for g in craft.groups] == ["all", "any"]
    assert [line.text for line in craft.groups[1].lines] == [
        "has an influence",
        "is fractured",
        "is synthesised",
    ]


def test_excluded_categories_are_shown_as_an_exception_and_do_not_create_a_section():
    """`high-life` excludes jewels and flasks. Manufacturing a Jewel section to
    list a rule that can never fire on a jewel would be worse than silence."""
    doc = explain.describe(
        ruleset(
            [
                {
                    "id": "life-but-not-jewels",
                    "score": 7,
                    "exclude_categories": ["jewel.base", "flask"],
                    "all": [{"pseudo": "life", "min": 90}],
                }
            ]
        )
    )
    assert [s.label for s in doc.sections] == [explain.EVERY_SLOT]
    assert [s.label for s in card(doc, "life-but-not-jewels").excluded] == ["Jewel", "Flask"]


def test_slot_sections_follow_the_category_order_and_put_every_slot_first(shipped):
    """One source of truth for slot order - `items.CATEGORY_LABELS` - so adding a
    category orders itself instead of needing a second list here."""
    labels = [s.label for s in shipped.sections]
    assert labels[0] == explain.EVERY_SLOT
    assert labels[1:] == [
        label
        for cid, label in explain.CATEGORY_LABELS.items()
        if any(cid in [s.id for s in c.slots] for c in shipped.rules)
    ]
    assert "Flask" not in labels, "no rule names flasks, so there is no flask section"


def test_a_universal_rule_is_listed_once_under_every_slot_and_not_per_slot(shipped):
    """One card per rule. Repeating them per section would turn 47 rules into 115
    appearances, 68 of them duplicates, and make a duplicate HTML id a bug to be
    tested for rather than an impossible one."""
    every = shipped.sections[0]
    assert {c.id for c in every.rules} == {c.id for c in shipped.rules if c.universal}
    for section in shipped.sections[1:]:
        assert all(not c.universal for c in section.rules)


def test_every_rule_and_veto_in_the_shipped_ruleset_is_described(shipped):
    raw = Ruleset.load()
    assert [c.id for c in shipped.rules] == [str(r["id"]) for r in raw.rules]
    assert [c.id for c in shipped.veto] == [str(r["id"]) for r in raw.veto]
    assert all(c.groups for c in shipped.cards), "a card with no conditions explains nothing"


def test_the_summary_order_is_score_descending_with_ties_broken_by_id(shipped):
    ordered = [(c.score, c.id) for c in shipped.by_score]
    assert ordered == sorted(ordered, key=lambda pair: (-pair[0], pair[1]))


def test_integer_scores_carry_no_decimal_point(shipped):
    """`score` is a float because YAML integers arrive as one; `7.0` in a table
    of hand-authored priors reads as a precision the numbers do not have."""
    assert card(shipped, "high-life").score_text == "7"
    assert explain.RuleCard(id="x", score=7.5, note="").score_text == "7.5"


def test_the_settings_name_the_fingerprint_and_the_gates_that_run_before_scoring(shipped):
    """A page about a ruleset that does not say *which* ruleset is the exact
    failure `triage.fingerprint` exists to prevent."""
    assert shipped.settings.fingerprint == Ruleset.load().fingerprint
    assert shipped.settings.promote_score == 10
    assert any("not rare" in gate for gate in shipped.settings.gates)
    assert any("item level below 60" in gate for gate in shipped.settings.gates)


def test_the_glossaries_define_every_name_a_condition_can_use(shipped):
    """A term the page cannot define is a condition the reader cannot check."""
    assert {t.name for t in shipped.pseudos} == set(triage.PSEUDO_NAMES)
    assert {t.name for t in shipped.flags} == set(triage.FLAG_NAMES)
    assert all(t.label and t.definition for t in shipped.pseudos + shipped.flags)


@pytest.mark.parametrize(
    "name,must_say",
    [
        ("life", "half"),  # effective life counts half of Strength
        ("increased_life", "%"),  # and the percentage roll is not part of it
        ("elemental_resistance", "three times"),  # all Elemental counts thrice
        ("count_elemental_resistances", "greater than zero"),  # a negative roll does not count
    ],
)
def test_the_pseudo_glossary_records_the_traps_and_not_just_the_names(shipped, name, must_say):
    """These three are the ones that get assumed wrongly. `#% increased maximum
    Life` lands in `increased_life` and *not* in `life`; `all Elemental` expands
    to three resistances while plain `all` expands to four; and the counts only
    count resistances that are positive."""
    term = next(t for t in shipped.pseudos if t.name == name)
    assert must_say in term.definition or must_say in term.unit


def test_a_multi_template_mod_condition_names_the_templates_it_counts():
    """Found by reading the rendered page: `minus-mana-cost` said "whichever of
    these 2 mods rolls highest" and never named either of them.

    A single template is quoted in the sentence, so `detail` stays empty rather
    than printing it twice; several are only *counted* there, so they have to be
    listed. That is the whole contract of `detail`, and this is the case it
    exists for.
    """
    both = [MANA_COST, "# to Total Mana Cost of Skills"]
    assert one({"mod_any": both, "min": 4}).detail == tuple(both)
    assert one({"mod": MANA_COST, "min": 4}).detail == ()


def test_the_mod_count_scalar_says_which_mods_it_counts(shipped):
    """"The item's mods" means three different things: `_best_mod` reads every
    mod including enchants, `pseudo.compute` reads four domains, and this counts
    two."""
    term = next(t for t in shipped.scalars if t.name == "mod_count")
    assert "explicit" in term.definition and "fractured" in term.definition
    assert "not counted" in term.definition


# -- the page ---------------------------------------------------------------


@pytest.fixture(scope="module")
def page(tmp_path_factory, shipped):
    """The shipped ruleset rendered to HTML, once.

    `out_path` is mandatory in every render test: the default writes into the
    developer's real `~/.poescan/reports`.
    """
    out = tmp_path_factory.mktemp("rules") / "rules.html"
    return report_mod.render_rules(shipped, out_path=out).read_text()


def test_every_rule_in_the_shipped_ruleset_renders_with_its_score_and_note_in_the_html(
    page, shipped
):
    """The user's actual ask: 47 rules and 2 vetoes, each with what it rewards
    and how much. The lint tests check the page is not lying; this checks it says
    anything at all."""
    for c in shipped.cards:
        assert f'id="{c.anchor}"' in page, c.id
        assert c.note in page, c.id
    for c in shipped.rules:
        assert f"<b>{c.score_text}</b>" in page, c.id


def test_every_rule_appears_as_exactly_one_card(page, shipped):
    """`weapon-attack-speed` applies to ten slots. Repeating a full card per slot
    would turn 47 rules into 115 appearances and make a duplicate HTML id a bug
    to be tested for; the slot index links back instead."""
    anchors = re.findall(r'id="(rule-[^"]+)"', page)
    assert len(anchors) == len(set(anchors)) == len(shipped.cards)


def test_every_link_on_the_page_lands_on_something_that_exists(page):
    """A summary table of 49 links is worthless if one of them goes nowhere."""
    ids = set(re.findall(r'id="([^"]+)"', page))
    targets = set(re.findall(r'href="#([^"]+)"', page))
    assert targets and not targets - ids


def test_the_vetoes_are_presented_before_the_scoring_rules(page):
    """They run before scoring, and a page that lists them last describes a
    different evaluation order from the one the code uses."""
    assert page.index('id="vetoes"') < page.index('id="rules"')
    assert page.index("mirrored - cannot be traded") < page.index("Tailwind boots")


def test_the_two_life_rules_read_as_a_band_on_the_page(page):
    """Rendered, not just modelled: the ceiling is in the headline and the `none`
    block is gone, so nothing on the page reads as a penalty for rolling well."""
    assert "effective life 90 or more, but under 115" in page
    assert "Must not have" not in page


def test_the_rules_page_says_the_score_is_a_filter_and_not_a_ranking(page):
    """A table of 49 numbers sorted descending reads as a ranking of value, which
    is the one claim this repo exists to deny. Measured, the top-scoring item
    (31) was worth 2c while items scoring 12 ran 1c-25c. Pinned so a future edit
    to the page cannot quietly drop the sentence that says so."""
    assert "not a valuation" in page
    assert "worth 2 chaos" in page and "1 to 25" in page
    assert "scarcity" in page


def test_the_rules_page_says_the_scores_are_hand_authored_priors(page):
    """"The 49 rule scores in default.yaml are hand-authored priors. Nothing
    measured them." Rendering them in a neat table without saying that is the
    second way this page could be the most persuasive wrong thing in the repo."""
    assert "written by hand" in page
    assert "Nothing measured them" in page
    assert "analyse" in page, "the reader needs the command that corrects them"


def test_the_rules_page_names_the_ruleset_fingerprint(page, shipped):
    """A page about a ruleset that does not say *which* ruleset is the exact
    failure `triage.fingerprint` exists to prevent: rules get retuned, and the
    page then describes a ruleset that no longer exists while reading as current.
    """
    assert shipped.settings.fingerprint in page


def test_the_rules_page_says_what_it_does_not_check(page):
    """A clean card has to mean "no problem found", not "not checked", so the
    page states the boundary and names the command that owns the rest."""
    assert "validate-rules" in page


def test_the_rules_page_is_self_contained(page):
    """No JavaScript, no external assets: a page that needs the network to
    render is a page that stops working, and this one documents an offline tool.
    """
    assert "<script" not in page
    assert "src=" not in page
    assert not re.search(r'(?:src|href)="https?:', page)


def test_a_regex_in_a_rule_cannot_inject_markup_into_the_page(tmp_path):
    """Rule text is rendered in three ways - plain, as a `<code>` literal, and
    through the backtick filter that adds markup of its own - so all three are
    checked. The filter escapes before it inserts a tag; if that order ever
    inverts, a ruleset becomes a way to write HTML."""
    doc = explain.describe(
        ruleset(
            [
                {
                    "id": "<script>alert(1)</script>",
                    "score": 5,
                    "note": "<img src=x onerror=alert(1)>",
                    "all": [
                        {"mod_matches": "<script>evil</script>"},
                        {"<script>key</script>": 1},
                    ],
                }
            ]
        )
    )
    html = report_mod.render_rules(doc, out_path=tmp_path / "x.html").read_text()
    assert "<script" not in html, "a rule id or a regex became a tag"
    assert "<img" not in html, "a note became a tag"
    assert "&lt;script&gt;" in html, "and it is still shown, as text"


def test_the_backtick_filter_escapes_before_it_adds_markup():
    """The unit behind the test above, since the failure is silent and total."""
    assert report_mod.codespans("a `b` c") == "a <code>b</code> c"
    assert report_mod.codespans("`<script>`") == "<code>&lt;script&gt;</code>"


def test_integer_scores_render_without_a_decimal_point(page):
    """`score` is a float because YAML integers arrive as one, so `7.0` would
    reach the page unless formatted - a spurious decimal on a hand-authored
    number reads as precision that does not exist."""
    assert "<b>7</b>" in page
    assert not re.search(r"<b>\d+\.0</b>", page)


# -- evidence ---------------------------------------------------------------


def obs(median, score=10.0, rules=(), base="Opal Ring", ruleset="abc123", total=10):
    """One row shaped like `Cache.observations` returns, per test_calibration.py."""
    return {
        "median": median,
        "cheapest": median,
        "total": total,
        "triage_score": score,
        "rules_hit": list(rules),
        "base_type": base,
        "ruleset": ruleset,
    }


@pytest.fixture
def rows():
    """Observations labelled by the shipped ruleset, with a tail and an anecdote."""
    fp = Ruleset.load().fingerprint
    made = [obs(p, rules=["fractured", "influenced"], ruleset=fp) for p in (1.0, 1.0, 202.5)]
    made += [obs(p, rules=["influenced"], ruleset=fp) for p in (4.0, 5.0, 6.0)]
    made += [obs(None, rules=["chaos-res"], ruleset=fp)]  # a check that found no price
    return made


def evidence(rows, **kw):
    return explain.evidence_from(rows, Ruleset.load(), **kw)


def test_evidence_reports_the_tail_beside_the_median_for_every_rule_row(rows):
    """Four numbers or none. A median column is what misled this repo for months:
    `fractured` medians 1c here and caught the 202.5c item, and only one of those
    two facts is a reason to keep the rule."""
    e = evidence(rows)
    g = e.measured("fractured")
    assert (g.n, g.median, g.best, g.tail) == (3, 1.0, 202.5, 1)
    assert g.threshold == cal.TAIL_THRESHOLD


def test_an_unpriced_check_is_not_counted_as_a_zero(rows):
    """A check that matched nothing is a fact about scarcity, not a price of 0."""
    e = evidence(rows)
    assert (e.observations, e.considered, e.priced) == (7, 7, 6)
    assert e.measured("chaos-res") is None, "its only observation had no price"


def test_a_rule_with_no_observations_reads_as_not_measured_rather_than_zero(rows, tmp_path):
    """An unmeasured rule has not been found worthless - it has not been measured.
    Rendering it as 0, or as a dash, says the first thing."""
    e = evidence(rows)
    assert "boots-tailwind" in e.never_fired
    assert e.measured("boots-tailwind") is None

    html = report_mod.render_rules(
        explain.describe(Ruleset.load()), evidence=e, out_path=tmp_path / "r.html"
    ).read_text()
    assert "not measured" in html
    assert "has not been found worthless" in html


def test_a_veto_is_reported_as_unmeasurable_rather_than_unmeasured(rows, tmp_path):
    """The page's most interesting sentence, and it is a fact about the code, not
    a hedge: `assess` returns before scoring when a veto matches and `_features`
    is only written for checked items, so `rules_hit` can never contain a veto id.
    `never_fired` does not even iterate them. The two vetoes are therefore the
    only rules whose false-negative cost this tool has no instrument for."""
    ruleset = Ruleset.load()
    veto_ids = {str(v["id"]) for v in ruleset.veto}
    e = evidence(rows)
    assert not (veto_ids & e.never_fired), "a veto cannot be reported as unmeasured"
    assert all(e.measured(v) is None for v in veto_ids)

    html = report_mod.render_rules(
        explain.describe(ruleset), evidence=e, out_path=tmp_path / "r.html"
    ).read_text()
    assert "cannot be measured" in html
    veto_card = html.split('id="rule-low-ilvl-jewellery"')[1].split('<div class="card ')[0]
    assert "cannot be measured" in veto_card
    assert "not measured" not in veto_card.replace("cannot be measured", "")


def test_a_thin_row_is_caveated_in_words_and_not_by_colour(rows, tmp_path):
    """Colour says "good" or "bad"; the problem with n=3 is neither."""
    e = evidence(rows)
    assert e.anecdote(e.measured("fractured"))
    html = report_mod.render_rules(
        explain.describe(Ruleset.load()), evidence=e, out_path=tmp_path / "r.html"
    ).read_text()
    assert "an anecdote, not a measurement" in html


def test_observations_from_another_ruleset_are_set_aside_and_the_page_says_so(rows, tmp_path):
    """A rule id means nothing without the ruleset that defined it. Pooling two
    vocabularies produces a table that looks like evidence and is not - which is
    how the nested life bands went on being reported after they were split."""
    foreign = [obs(500.0, rules=["very-high-life"], ruleset="deadbeef")]
    e = evidence(rows + foreign)
    assert (e.observations, e.considered, e.setaside) == (8, 7, 1)
    assert ("deadbeef", 1) in e.vocabularies
    assert not e.pooled

    html = report_mod.render_rules(
        explain.describe(Ruleset.load()), evidence=e, league="Allflame", out_path=tmp_path / "r.html"
    ).read_text()
    assert "1 of 8 observations set aside" in html
    assert "deadbeef" in html
    assert "zero API calls" in html, "the actionable half: a scan re-labels them for free"


def test_pooling_foreign_observations_is_possible_and_flagged(rows, tmp_path):
    e = evidence(rows + [obs(500.0, rules=["very-high-life"], ruleset="deadbeef")], pooled=True)
    assert (e.considered, e.setaside, e.pooled) == (8, 1, True)
    html = report_mod.render_rules(
        explain.describe(Ruleset.load()), evidence=e, out_path=tmp_path / "r.html"
    ).read_text()
    assert "Pooling 1 observations from other rulesets" in html


def test_rules_that_fired_together_are_counted_from_observations_not_inferred_from_thresholds(rows):
    """Static detection of stacking rules was built and cut: on this ruleset the
    obvious heuristic yields six candidate pairs, four wrong, and it misses the
    largest real interaction (`influenced` + `double-influenced` = 26 on any
    double-influenced item) because the selectors differ. Counting `rules_hit` has
    no false positives by construction."""
    e = evidence(rows)
    assert e.together == (("fractured", "influenced", 3),)


def test_the_median_is_the_only_number_that_carries_a_tone(rows):
    """`n`, `best` and the tail count are facts about sample size and what was
    caught; a colour on them would imply a judgement only the baseline supports."""
    e = evidence(rows)
    assert e.baseline == 4.5
    assert explain.price_tone(20.0, e.baseline) == "good"
    assert explain.price_tone(1.0, e.baseline) == "bad"
    assert explain.price_tone(6.0, e.baseline) == ""
    assert explain.price_tone(None, e.baseline) == ""
    assert explain.price_tone(500.0, None) == "", "no baseline is not a judgement"


def test_both_renderers_read_one_good_multiple(monkeypatch):
    """`analyse` colours a terminal and this page names a CSS class - two media,
    deliberately not one function. The *threshold* is shared, so the two commands
    cannot start disagreeing about which median is good news."""
    assert cal.GOOD_MULTIPLE == 2.0
    monkeypatch.setattr(cal, "GOOD_MULTIPLE", 10.0)
    assert explain.price_tone(30.0, 10.0) == ""
    assert "green" not in cli._price_cell(30.0, 10.0)


def test_the_evidence_section_still_renders_with_no_observations_at_all(tmp_path):
    """The page always renders in full: evidence is a column, never a gate."""
    e = evidence([])
    assert (e.observations, e.priced, e.baseline) == (0, 0, None)
    html = report_mod.render_rules(
        explain.describe(Ruleset.load()), evidence=e, out_path=tmp_path / "r.html"
    ).read_text()
    assert "Nothing here is priced yet" in html
    assert "boots-tailwind" in html, "every rule is still documented"


def test_every_rule_is_still_documented_when_there_is_no_evidence_at_all(tmp_path, shipped):
    """`--no-evidence`, or an unreadable cache. A documentation page must not fail
    because a database is locked."""
    html = report_mod.render_rules(shipped, evidence=None, out_path=tmp_path / "r.html").read_text()
    assert "No evidence was read for this page" in html
    for c in shipped.cards:
        assert c.note in html


def test_an_unreadable_cache_degrades_to_no_evidence_rather_than_failing(
    tmp_path, monkeypatch, capsys, no_browser
):
    """The cache is not this command's subject, so it cannot be its failure mode."""
    import poescan.cache as cache_mod

    def explode(*a, **kw):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(cache_mod, "Cache", explode)
    monkeypatch.setattr(cli, "Cache", explode)
    args = _RulesArgs()
    args.out = str(tmp_path / "rules.html")
    assert cli.cmd_explain_rules(args) == 0
    assert "No evidence available" in capsys.readouterr().out
    assert "No evidence was read for this page" in (tmp_path / "rules.html").read_text()



# -- the command ------------------------------------------------------------


class _RulesArgs:
    """argparse's namespace for `poescan explain-rules`, with its defaults."""

    rules = out = league = None
    no_open = True
    no_evidence = False


@pytest.fixture
def no_browser(monkeypatch):
    """Records browser opens instead of performing them."""
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    return opened


def test_explain_rules_writes_a_page_and_does_not_open_a_browser_when_told_not_to(
    tmp_path, no_browser
):
    args = _RulesArgs()
    args.out = str(tmp_path / "rules.html")
    assert cli.cmd_explain_rules(args) == 0
    assert "how items are scored" in (tmp_path / "rules.html").read_text()
    assert no_browser == []

    args.no_open = False
    assert cli.cmd_explain_rules(args) == 0
    assert no_browser == [(tmp_path / "rules.html").as_uri()]


def test_no_evidence_reads_no_cache_at_all(tmp_path, monkeypatch, no_browser):
    """The cold-machine path: someone documenting a ruleset before they have ever
    run a scan should not need a database to exist."""
    def explode(*a, **kw):
        raise AssertionError("--no-evidence must not open the cache")

    monkeypatch.setattr(cli, "Cache", explode)
    args = _RulesArgs()
    args.out, args.no_evidence = str(tmp_path / "rules.html"), True
    assert cli.cmd_explain_rules(args) == 0
    assert "No evidence was read for this page" in (tmp_path / "rules.html").read_text()


def test_the_league_flag_chooses_whose_observations_are_read(tmp_path, monkeypatch, no_browser):
    """Observations are per league, and a page headed with one league while
    counting another's prices would be wrong in the least visible way."""
    asked: list[str] = []

    class FakeCache:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def observations(self, league):
            asked.append(league)
            return []

    monkeypatch.setattr(cli, "Cache", FakeCache)
    args = _RulesArgs()
    args.out, args.league = str(tmp_path / "rules.html"), "Settlers"
    assert cli.cmd_explain_rules(args) == 0
    assert asked == ["Settlers"]
    assert "Settlers" in (tmp_path / "rules.html").read_text()


def test_the_configured_league_is_used_when_none_is_named(tmp_path, monkeypatch, no_browser):
    asked: list[str] = []

    class FakeCache:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def observations(self, league):
            asked.append(league)
            return []

    monkeypatch.setattr(cli, "Cache", FakeCache)
    args = _RulesArgs()
    args.out = str(tmp_path / "rules.html")
    assert cli.cmd_explain_rules(args) == 0
    assert asked == ["Allflame"], "from Config, which the fixture stubs"


def test_explain_rules_never_loads_the_trade_stat_definitions(tmp_path, monkeypatch, no_browser):
    """Stronger than "makes no requests", which the `offline` fixture already
    enforces: the command must work on a machine with no vendored metadata and no
    cached download at all. Documenting a ruleset does not need the stat index,
    and reaching for it would put a network call on a documentation command."""
    def explode():
        raise AssertionError("explain-rules must not load the trade stat definitions")

    monkeypatch.setattr(cli, "load_stat_index", explode)
    args = _RulesArgs()
    args.out = str(tmp_path / "rules.html")
    assert cli.cmd_explain_rules(args) == 0


def test_a_ruleset_with_a_dead_condition_still_renders_and_still_exits_zero(
    tmp_path, capsys, no_browser
):
    """This is the only tool that renders a dead rule *visibly*, so exiting
    non-zero would make it unusable in the middle of the edit that broke it.
    `validate-rules` owns the exit code."""
    path = tmp_path / "broken.yaml"
    path.write_text(
        "settings: {promote_score: 10}\n"
        "rules:\n"
        "  - id: dead\n"
        "    score: 5\n"
        "    all:\n"
        '      - {psuedo: life, min: 90}\n'
        '      - {ilvl: "86"}\n'
    )
    args = _RulesArgs()
    args.rules, args.out = str(path), str(tmp_path / "rules.html")
    assert cli.cmd_explain_rules(args) == 0

    out = capsys.readouterr().out
    assert "dead" in out and "validate-rules" in out
    html = (tmp_path / "rules.html").read_text()
    assert "never true for any item" in html
    assert "problem" in html


def test_a_ruleset_that_will_not_load_names_the_command_that_diagnoses_it(tmp_path, capsys):
    """`Ruleset.load` refuses a condition naming two selectors. The useful reply
    is not the traceback but the command that lists every such problem at once.
    """
    path = tmp_path / "ambiguous.yaml"
    path.write_text(
        "rules:\n  - id: two-selectors\n    score: 5\n"
        "    all:\n      - {base: Opal Ring, ilvl: {min: 84}}\n"
    )
    args = _RulesArgs()
    args.rules, args.out = str(path), str(tmp_path / "rules.html")
    assert cli.cmd_explain_rules(args) == 1

    out = capsys.readouterr().out
    assert "two-selectors" in out and "validate-rules" in out
    assert not (tmp_path / "rules.html").exists(), "a page was written for a ruleset that failed"


def test_a_missing_ruleset_file_is_reported_rather_than_raised(tmp_path, capsys):
    args = _RulesArgs()
    args.rules, args.out = str(tmp_path / "nope.yaml"), str(tmp_path / "rules.html")
    assert cli.cmd_explain_rules(args) == 1
    assert "Could not read" in capsys.readouterr().out


def test_a_ruleset_with_no_rules_is_refused_rather_than_rendered_blank(tmp_path, capsys):
    """A settings-only file is a plausible `--rules` mistake, and an empty page
    documenting nothing looks like the tool working."""
    path = tmp_path / "empty.yaml"
    path.write_text("settings: {promote_score: 10}\n")
    args = _RulesArgs()
    args.rules, args.out = str(path), str(tmp_path / "rules.html")
    assert cli.cmd_explain_rules(args) == 1
    assert "nothing to document" in capsys.readouterr().out
    assert not (tmp_path / "rules.html").exists()


def test_explain_rules_is_reachable_from_the_command_line_parser():
    """The command exists only if `build_parser` knows about it."""
    args = cli.build_parser().parse_args(["explain-rules", "--no-open"])
    assert args.func is cli.cmd_explain_rules
    assert args.no_open and args.rules is None and args.out is None
    assert not args.no_evidence and args.league is None
