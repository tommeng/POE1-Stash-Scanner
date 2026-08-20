"""The ruleset, read back as English: the model behind `poescan explain-rules`.

Pure functions over frozen dataclasses, with no I/O and no templating, mirroring
the split `calibration.py` (the maths) and `cli.cmd_analyse` (the rendering)
already use. Nothing here reads the cache, the network or the trade metadata, so
the page it feeds works on a cold machine.

Two things this module is careful about, because both would make a
confident-looking page wrong.

**It says what the evaluator does, not what the rule meant.** A condition that
is satisfied by every item is described as such, and `abs: true` is described as
comparison on absolute value rather than as "this mod rolls negative" - which is
the plausible reading and is false: `minus-mana-cost` scores 26 on a *positive*
Total Mana Cost roll. Repairing the ruleset is a measured decision and does not
belong to a documentation command.

**Its lint stops exactly where `validate-rules` starts.** That command already
catches unknown flags, pseudo fields, categories, mod templates, stat ids, base
types and regexes. What it structurally cannot see is a condition that is
*well-formed and dead*: `{ilvl: "86"}` is never true, `{ilvl: {}}` is always
true, and a `min` beside a scalar is silently ignored - all three are built from
keys that are in `CONDITION_KEYS`, so a key-set check finds nothing. Those four
classes are what the findings here report, and `validate-rules` keeps ownership
of the exit code.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace

from . import calibration, triage
from .items import CATEGORY_LABELS

# The glossary lives next to the getters it describes, in `triage`, so that
# adding a pseudo field puts the missing definition in the author's own diff.
from .triage import Term, _FLAG_LABELS, _PSEUDO_LABELS, _SCALAR_LABELS

# Finding levels. "problem" means the condition can never be satisfied, so the
# rule is dead; "caution" means it is satisfied more widely than it reads.
PROBLEM = "problem"
CAUTION = "caution"

# The lead-in for each condition section. `none` reads as an exclusion and never
# as "must not have": several rules use it purely to carve disjoint bands, and
# "must not have 115 life" reads as a penalty for rolling well.
LEADS = {"all": "Needs all of", "any": "Needs any of", "none": "Excluded when"}

EVERY_SLOT = "Every slot"

_ABS_CLAUSE = (
    "compared on absolute value, so a positive roll of the same size also "
    "satisfies it"
)


# --------------------------------------------------------------------------
# The documented shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """Something true about a condition that its author probably did not mean."""

    level: str
    text: str

    @property
    def is_problem(self) -> bool:
        return self.level == PROBLEM


@dataclass(frozen=True)
class ConditionLine:
    """One condition, in English.

    ``findings`` is a tuple rather than a problem string and a caution string:
    ``{ilvl: {}, min: 86}`` is always true *and* ignores a modifier, and a page
    that could only show one of those would show the less important one.

    ``detail`` holds exactly the strings ``text`` refers to but does not spell
    out: the two templates behind "whichever of these 2 mods", or the literal
    regex behind a paraphrase. It is empty when the sentence already quotes them
    verbatim, so a page can render it unconditionally without either printing
    everything twice or - the failure this contract was written for - saying
    "these 2 mods" and never naming them.
    """

    text: str
    family: frozenset[str]
    detail: tuple[str, ...] = ()
    is_pattern: bool = False
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class Slot:
    """An item category a rule can be restricted to."""

    id: str
    label: str

    @property
    def anchor(self) -> str:
        return f"slot-{slug(self.id)}"


@dataclass(frozen=True)
class ClauseGroup:
    kind: str  # one of triage.CONDITION_SECTIONS
    lead: str
    lines: tuple[ConditionLine, ...]


@dataclass(frozen=True)
class BandNote:
    """One half of a pair of rules deliberately written as disjoint bands."""

    partner_id: str
    lower: bool
    text: str


@dataclass(frozen=True)
class RuleCard:
    id: str
    score: float
    note: str
    slots: tuple[Slot, ...] = ()
    excluded: tuple[Slot, ...] = ()
    groups: tuple[ClauseGroup, ...] = ()
    band: BandNote | None = None
    # The rule as written. Carried so that `bands` can compare conditions as
    # dicts - the only reliable way to recognise a band boundary - without
    # re-parsing the English back out of a ConditionLine.
    source: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def universal(self) -> bool:
        """True when no `categories` key restricts the rule to some slots."""
        return not self.slots

    @property
    def anchor(self) -> str:
        return f"rule-{slug(self.id)}"

    @property
    def score_text(self) -> str:
        """The score without a trailing `.0`; YAML integers arrive as floats."""
        return _num(self.score)

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(f for g in self.groups for line in g.lines for f in line.findings)


@dataclass(frozen=True)
class SlotSection:
    slot: Slot | None  # None is the "Every slot" section
    label: str
    anchor: str
    rules: tuple[RuleCard, ...]


@dataclass(frozen=True)
class SettingsDoc:
    promote_score: float
    min_ilvl: int
    max_market_checks: int
    fingerprint: str
    order: tuple[str, ...]  # how an item is judged, in order
    gates: tuple[str, ...]  # the rejects that run before the vetoes


@dataclass(frozen=True)
class TermDoc:
    name: str
    label: str
    definition: str
    unit: str = ""


@dataclass(frozen=True)
class Evidence:
    """What the accumulated market checks say about this ruleset's rules.

    Every number here was paid for by a scan the user already ran. Note what is
    *absent*: there is no entry for a veto, and there cannot be one. `assess`
    returns before scoring when a veto matches, `_features` is only written for
    items that got checked, so `rules_hit` can never contain a veto id - and
    `calibration.never_fired` does not even iterate them.
    """

    observations: int
    considered: int
    priced: int
    setaside: int
    vocabularies: tuple[tuple[str, int], ...]
    baseline: float | None
    by_rule: dict[str, calibration.Group]
    never_fired: frozenset[str]
    together: tuple[tuple[str, str, int], ...]
    threshold: float = calibration.TAIL_THRESHOLD
    pooled: bool = False

    @property
    def thin(self) -> bool:
        """Too few priced observations to calibrate from, at any granularity."""
        return self.priced < 30

    def measured(self, rule_id: str) -> calibration.Group | None:
        return self.by_rule.get(rule_id)

    def tone(self, median: float | None) -> str:
        """The CSS class for a median of this evidence's own baseline."""
        return price_tone(median, self.baseline)

    def anecdote(self, group: calibration.Group | None) -> bool:
        """A row too thin to be a measurement, which is said in words, not colour."""
        return group is not None and group.n < ANECDOTE_BELOW


@dataclass(frozen=True)
class RulesetDoc:
    settings: SettingsDoc
    veto: tuple[RuleCard, ...]
    rules: tuple[RuleCard, ...]
    sections: tuple[SlotSection, ...]
    pseudos: tuple[TermDoc, ...]
    flags: tuple[TermDoc, ...]
    scalars: tuple[TermDoc, ...]

    @property
    def cards(self) -> tuple[RuleCard, ...]:
        """Every card, vetoes first - they run before scoring."""
        return self.veto + self.rules

    @property
    def by_score(self) -> tuple[RuleCard, ...]:
        """The scoring rules, highest first, ties broken by id.

        Deliberately not a ranking of *value*: measured, the top-scoring item in
        the author's cache (31) was worth 2c while items scoring 12 ran 1c-25c.
        The page carries that disclaimer beside this table.
        """
        return tuple(sorted(self.rules, key=lambda c: (-c.score, c.id)))

    @property
    def problems(self) -> tuple[tuple[str, Finding], ...]:
        return self._findings(PROBLEM)

    @property
    def cautions(self) -> tuple[tuple[str, Finding], ...]:
        return self._findings(CAUTION)

    def _findings(self, level: str) -> tuple[tuple[str, Finding], ...]:
        return tuple(
            (card.id, f) for card in self.cards for f in card.findings if f.level == level
        )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def slug(text: str) -> str:
    """A stable HTML fragment id for a rule id, slot id or label."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "unnamed"


def _num(value: float) -> str:
    return f"{float(value):g}"


def _literal(value) -> str:
    """A value as the YAML author wrote it, quotes and all - the point of it."""
    return f"`{value!r}`"


def _as_list(value) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _negate(predicate: str) -> str:
    """Turn an affirmative flag label into its negation."""
    if predicate.startswith("is "):
        return "is not " + predicate[3:]
    if predicate.startswith("has "):
        return "does not have " + predicate[4:]
    return f"not {predicate}"


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


def threshold_phrase(spec, *, noun: str, unit: str = "") -> tuple[str, tuple[Finding, ...]]:
    """The bound a threshold spec puts on a value, in English, plus any findings.

    Both spellings ``triage._in_range`` accepts are handled: a bare number,
    which means ">=", and a ``min``/``max`` mapping. Anything else - a string, a
    list, ``None`` - makes it return False for every item, so the condition is
    dead; ``{ilvl: "86"}`` is the easiest YAML typo there is and is invisible to
    `validate-rules`, because `ilvl` is a perfectly good condition key.

    An unbounded mapping returns no phrase and no finding: whether that is
    always-true depends on the family, and only the caller knows. ``{pseudo:
    life}`` matches every item, while ``{mod: X}`` correctly does not.
    """
    if isinstance(spec, bool):
        # YAML `true` reaches `_in_range` as a number. Say so, rather than
        # guessing what it was for.
        return (
            f"{int(spec)}{unit} or more",
            (
                Finding(
                    CAUTION,
                    f"the {noun} threshold is `{str(spec).lower()}`, which is "
                    f"compared as the number {int(spec)}",
                ),
            ),
        )
    if isinstance(spec, (int, float)):
        return f"{_num(spec)}{unit} or more", ()
    if not isinstance(spec, dict):
        return "", (
            Finding(
                PROBLEM,
                f"the {noun} threshold is {_literal(spec)}, which is neither a "
                "number nor a `min`/`max` pair, so this condition is never true "
                "for any item",
            ),
        )

    bounds: dict[str, float] = {}
    broken: list[Finding] = []
    for key in ("min", "max"):
        if key not in spec:
            continue
        try:
            bounds[key] = float(spec[key])
        except (TypeError, ValueError):
            broken.append(
                Finding(
                    PROBLEM,
                    f"the `{key}` bound of the {noun} threshold is "
                    f"{_literal(spec[key])}, which is not a number - scoring an "
                    "item against this condition raises rather than matching",
                )
            )
    if broken:
        return "", tuple(broken)

    low, high = bounds.get("min"), bounds.get("max")
    if low is not None and high is not None:
        # Both bounds stated literally rather than as "under 160": `max: 159`
        # admits 159.5, so the arithmetic form would be wrong for a fractional
        # roll. The "but under" wording belongs to `bands`, where the ceiling
        # really is a `min` on the neighbouring rule.
        return f"{_num(low)}{unit} or more, and {_num(high)}{unit} or less", ()
    if low is not None:
        return f"{_num(low)}{unit} or more", ()
    if high is not None:
        return f"{_num(high)}{unit} or less", ()
    return "", ()


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

_DIGIT_CLASS = re.compile(r"\\d(\{\d+(,\d*)?\}|[*+?])?")
# Only escaped *punctuation* is literal text. `\s`, `\w` and friends mean
# something the paraphrase does not try to say, so they are left verbatim.
_ESCAPE = re.compile(r"\\([^A-Za-z0-9])")
_ALTERNATION = re.compile(r"\((\?:)?([^()|]+(?:\|[^()|]+)+)\)")
_GROUP = re.compile(r"\((\?:)?([^()|]*)\)")
_WILDCARD = re.compile(r"\.[*+]\??")
_STASHED = re.compile(r"\x00(\d+)\x00")


def _plain(m: re.Match, unwrapped: str) -> str:
    """``unwrapped``, unless the group is a lookaround or otherwise not literal.

    ``(?:A|B)`` is a group of alternatives and reads as "A or B"; ``(?<=A)B``
    asserts something about the text around it, which is not something this
    paraphrase tries to say, so it is left exactly as written.
    """
    if m.group(1) is None and m.group(2).startswith("?"):
        return m.group(0)
    return unwrapped


def paraphrase_pattern(pattern: str) -> str:
    """A `mod_matches` regex as something a reader can recognise.

    ``r"Critical Strike (Multiplier|Chance)"`` becomes "Critical Strike
    Multiplier or Chance". Purely for reading: the exact pattern always travels
    alongside in ``ConditionLine.detail``, because that is what you copy when
    editing the YAML.

    Cannot raise, and never claims more than it knows - anything it does not
    recognise is left verbatim, including a pattern that does not compile.
    Deliberately does *not* enumerate the trade stats a pattern matches: that
    needs the stat index, and this must work with no vendored metadata at all.
    """
    text = str(pattern)
    if text.startswith("^"):
        text = text[1:]
    if text.endswith("$") and not text.endswith("\\$"):
        text = text[:-1]

    text = _DIGIT_CLASS.sub("#", text)

    # Escaped metacharacters are literal text and must survive the structural
    # rewrites below, which would otherwise read `\(` as the start of a group.
    literals: list[str] = []

    def stash(m: re.Match) -> str:
        literals.append(m.group(1))
        return f"\x00{len(literals) - 1}\x00"

    text = _ESCAPE.sub(stash, text)
    text = _ALTERNATION.sub(
        lambda m: _plain(m, " or ".join(part.strip() for part in m.group(2).split("|"))), text
    )
    text = _GROUP.sub(lambda m: _plain(m, m.group(2)), text)
    text = _WILDCARD.sub(" … ", text)
    text = _STASHED.sub(
        lambda m: literals[int(m.group(1))] if int(m.group(1)) < len(literals) else "", text
    )
    return " ".join(text.split())


def _pattern_findings(pattern: str) -> tuple[Finding, ...]:
    try:
        re.compile(pattern)
    except re.error as e:
        return (
            Finding(
                PROBLEM,
                f"the pattern does not compile ({e}), so scoring any item "
                "against this condition raises rather than matching",
            ),
        )
    return ()


# --------------------------------------------------------------------------
# Selector -> English
# --------------------------------------------------------------------------

_MODIFIERS = ("min", "max", "abs", "is")

_SCALAR_FAMILIES = tuple(frozenset({name}) for name in _SCALAR_LABELS)

# Which of `min`/`max`/`abs`/`is` each family's branch of `evaluate_condition`
# actually reads. Read off that code, and pinned empirically by
# `test_the_declared_modifier_consumption_matches_what_the_evaluator_actually_reads`
# rather than trusted, because it is what the ignored-modifier finding is
# derived from. Note the scalars consume nothing here: their bound is nested
# under the selector key, so a *top-level* `min` beside `ilvl` does nothing.
_CONSUMES: dict[frozenset[str], frozenset[str]] = {
    frozenset({"pseudo"}): frozenset({"min", "max"}),
    frozenset({"mod", "mod_any"}): frozenset({"min", "max", "abs"}),
    frozenset({"mod_matches"}): frozenset({"min", "max", "abs"}),
    frozenset({"stat", "stat_any"}): frozenset({"min", "max", "abs"}),
    frozenset({"flag"}): frozenset({"is"}),
    frozenset({"base", "base_any"}): frozenset(),
    frozenset({"base_matches"}): frozenset(),
    frozenset({"category"}): frozenset(),
    **{family: frozenset() for family in _SCALAR_FAMILIES},
}


def _bounded_line(
    *, subject: str, spec, term: Term, family: frozenset[str], selector: str
) -> ConditionLine:
    """A line for a family whose whole content is "this quantity, bounded"."""
    phrase, findings = threshold_phrase(spec, noun=term.label, unit=term.unit)
    if phrase:
        return ConditionLine(text=f"{subject} {phrase}", family=family, findings=findings)
    if findings:
        return ConditionLine(
            text=f"{subject}, with an unusable threshold", family=family, findings=findings
        )
    return ConditionLine(
        text=f"any {subject} at all",
        family=family,
        findings=(
            Finding(
                CAUTION,
                f"`{selector}` names no `min` or `max`, so this condition is true "
                "for every item and its rule scores unconditionally",
            ),
        ),
    )


def _render_pseudo(cond: dict) -> ConditionLine:
    family = frozenset({"pseudo"})
    name = str(cond["pseudo"])
    term = _PSEUDO_LABELS.get(name)
    if term is None:
        return ConditionLine(
            text=f"pseudo total `{name}`",
            family=family,
            findings=(
                Finding(
                    PROBLEM,
                    f"`{name}` is not a pseudo total the evaluator computes, so "
                    "this condition never matches any item",
                ),
            ),
        )
    return _bounded_line(
        subject=term.label, spec=cond, term=term, family=family, selector=f"pseudo: {name}"
    )


def _hash_count(templates: Sequence[str]) -> int:
    """How many rolled numbers the templates share, or 0 if they disagree."""
    counts = {t.count("#") for t in templates}
    return counts.pop() if len(counts) == 1 else 0


def _implied_total(cond: dict, numbers: int) -> str:
    """What a `min` on an averaged roll comes to in total, or "" if there is none."""
    try:
        return _num(float(cond["min"]) * numbers)
    except (KeyError, TypeError, ValueError):
        return ""


def _roll_clause(cond: dict, phrase: str, templates: Sequence[str]) -> str:
    """How a bound on a matched mod reads, once averaging is accounted for.

    ``Mod.value`` is the *mean* of a multi-number roll for every family that
    compares one, so `min: 11` on `Adds # to # Physical Damage` is a bar on the
    average and must say so - and say what it comes to in total, which is the
    number the reader is actually thinking of.
    """
    numbers = _hash_count(templates)
    if numbers < 2:
        return f", rolling {phrase}"
    total = _implied_total(cond, numbers)
    return f", averaging {phrase}" + (f" (about {total} in total)" if total else "")


def _render_mod(cond: dict) -> ConditionLine:
    family = frozenset({"mod", "mod_any"})
    templates = _unique(
        str(t) for t in _as_list(cond.get("mod")) + _as_list(cond.get("mod_any"))
    )
    if not templates:
        return ConditionLine(
            text="a mod condition naming no mod at all",
            family=family,
            findings=(
                Finding(PROBLEM, "no mod template is listed, so nothing can ever match"),
            ),
        )

    phrase, findings = threshold_phrase(cond, noun="roll")
    if len(templates) == 1:
        quoted = f'"{templates[0]}"'
        text = f"has {quoted}" if phrase else f"carries {quoted}"
    elif phrase:
        # Not "any of these": `mod_any` takes the single highest-magnitude
        # matching mod and tests *it* against the bound, which is a different
        # claim from a multi-entry `any:` block, where each entry is a whole
        # condition in its own right.
        text = f"has whichever of these {len(templates)} mods rolls highest"
    else:
        text = f"carries at least one of these {len(templates)} mods"

    if phrase:
        text += _roll_clause(cond, phrase, templates)
    if cond.get("abs"):
        text += f"; {_ABS_CLAUSE}"
    # One template is quoted in the sentence; several are only counted there.
    listed = () if len(templates) == 1 else templates
    return ConditionLine(text=text, family=family, detail=listed, findings=findings)


def _render_mod_matches(cond: dict) -> ConditionLine:
    family = frozenset({"mod_matches"})
    pattern = str(cond["mod_matches"])
    phrase, findings = threshold_phrase(cond, noun="roll")
    text = f'has a mod matching "{paraphrase_pattern(pattern)}"'
    if phrase:
        text += _roll_clause(cond, phrase, (pattern,))
    text += " - a pattern, so it covers every mod of that family"
    if cond.get("abs"):
        text += f"; {_ABS_CLAUSE}"
    return ConditionLine(
        text=text,
        family=family,
        detail=(pattern,),
        is_pattern=True,
        findings=findings + _pattern_findings(pattern),
    )


def _render_flag(cond: dict) -> ConditionLine:
    family = frozenset({"flag"})
    name = str(cond["flag"])
    wanted = bool(cond.get("is", True))
    term = _FLAG_LABELS.get(name)
    if term is not None:
        return ConditionLine(
            text=term.label if wanted else _negate(term.label), family=family
        )
    if wanted:
        return ConditionLine(
            text=f"has the flag `{name}`",
            family=family,
            findings=(
                Finding(
                    PROBLEM,
                    f"`{name}` is not a flag the evaluator knows, so this "
                    "condition never matches any item",
                ),
            ),
        )
    return ConditionLine(
        text=f"does not have the flag `{name}`",
        family=family,
        findings=(
            Finding(
                CAUTION,
                f"`{name}` is not a flag the evaluator knows, and an unknown flag "
                "reads as false - so `is: false` makes this condition true for "
                "every item",
            ),
        ),
    )


def _scalar_renderer(name: str) -> Callable[[dict], ConditionLine]:
    term = _SCALAR_LABELS[name]
    family = frozenset({name})

    def render(cond: dict) -> ConditionLine:
        # `{ilvl: {min: 86}}` and `{ilvl: 86}` are the same condition, and read
        # the same here: `_in_range` treats a bare number as ">=".
        return _bounded_line(
            subject=term.label, spec=cond[name], term=term, family=family, selector=name
        )

    return render


def _generic_renderer(
    family: frozenset[str], *, is_pattern: bool = False
) -> Callable[[dict], ConditionLine]:
    """One honest rendering for the families the shipped ruleset never uses.

    `stat`, `base`, `base_matches` and `category` get this rather than four
    bespoke sentences with no reader. The coverage test accepts a bespoke
    renderer *or* an explicit generic entry, so a selector added to the
    evaluator can never render as "unknown".
    """
    keys = tuple(sorted(family))
    bounded = bool(_CONSUMES[family] & {"min", "max"})

    def render(cond: dict) -> ConditionLine:
        present = [k for k in keys if k in cond]
        values = tuple(str(v) for k in present for v in _as_list(cond.get(k)))
        text = "condition on " + " and ".join(f"`{k}`" for k in present)
        if values:
            text += ": " + ", ".join(values)
        findings: tuple[Finding, ...] = ()
        if bounded:
            phrase, findings = threshold_phrase(cond, noun="roll")
            if phrase:
                text += f", at {phrase}"
        # The values are already in the sentence, so `detail` stays empty.
        return ConditionLine(
            text=text, family=family, is_pattern=is_pattern, findings=findings
        )

    return render


_RENDERERS: dict[frozenset[str], Callable[[dict], ConditionLine]] = {
    frozenset({"pseudo"}): _render_pseudo,
    frozenset({"mod", "mod_any"}): _render_mod,
    frozenset({"mod_matches"}): _render_mod_matches,
    frozenset({"flag"}): _render_flag,
    frozenset({"stat", "stat_any"}): _generic_renderer(frozenset({"stat", "stat_any"})),
    frozenset({"base", "base_any"}): _generic_renderer(frozenset({"base", "base_any"})),
    frozenset({"base_matches"}): _generic_renderer(
        frozenset({"base_matches"}), is_pattern=True
    ),
    frozenset({"category"}): _generic_renderer(frozenset({"category"})),
    **{family: _scalar_renderer(name) for name, family in zip(_SCALAR_LABELS, _SCALAR_FAMILIES)},
}


# --------------------------------------------------------------------------
# The lint
# --------------------------------------------------------------------------


def _family_of(cond: dict) -> frozenset[str] | None:
    for family in triage.SELECTOR_FAMILIES:
        if family & cond.keys():
            return family
    return None


def _stray_keys(cond: dict) -> tuple[Finding, ...]:
    return tuple(
        Finding(
            PROBLEM,
            f"`{key}` is not a condition key the evaluator reads. A condition key "
            "outside the evaluator's set matches nothing and fails silently, "
            "forever.",
        )
        for key in sorted(set(cond) - triage.CONDITION_KEYS)
    )


def _ignored_modifiers(cond: dict, family: frozenset[str]) -> tuple[Finding, ...]:
    named = ", ".join(f"`{k}`" for k in sorted(family & cond.keys()))
    hint = (
        " A scalar takes its bound nested under the selector: `{ilvl: {min: 86}}`."
        if family in _SCALAR_FAMILIES
        else ""
    )
    return tuple(
        Finding(
            CAUTION,
            f"`{mod}` is not read by a {named} condition, so it does nothing here "
            f"- the condition is evaluated exactly as if it were absent.{hint}",
        )
        for mod in _MODIFIERS
        if mod in cond and mod not in _CONSUMES[family]
    )


def _conflict_findings(cond: dict) -> tuple[Finding, ...]:
    """A condition naming two selectors, reported without naming a winner.

    `triage.conflicting_selectors` returns its keys sorted, so it cannot say
    which one `evaluate_condition` would read, and duplicating that if-chain
    here to find out would be a second copy of the scoring order. `Ruleset.load`
    refuses such a ruleset anyway, so this is reachable only through the public
    `describe_condition`.
    """
    keys = triage.conflicting_selectors(cond)
    if not keys:
        return ()
    return (
        Finding(
            PROBLEM,
            "this condition names more than one selector ("
            + ", ".join(f"`{k}`" for k in keys)
            + "); only one of them is evaluated and the rest are silently "
            "ignored, so the ruleset is refused when it loads",
        ),
    )


def describe_condition(cond: dict) -> ConditionLine:
    """One condition as a line of English, with anything dead about it noted."""
    if not isinstance(cond, dict):
        return ConditionLine(
            text=f"{cond!r}",
            family=frozenset(),
            findings=(
                Finding(
                    PROBLEM,
                    "a condition that is not a `key: value` mapping matches nothing",
                ),
            ),
        )
    family = _family_of(cond)
    if family is None:
        return ConditionLine(
            text="a condition naming no selector at all",
            family=frozenset(),
            findings=(
                Finding(
                    PROBLEM,
                    "nothing here names a selector the evaluator reads, so this "
                    "condition never matches any item",
                ),
            )
            + _stray_keys(cond),
        )
    line = _RENDERERS[family](cond)
    extra = _ignored_modifiers(cond, family) + _conflict_findings(cond) + _stray_keys(cond)
    return replace(line, findings=line.findings + extra)


# --------------------------------------------------------------------------
# Rules, bands and slots
# --------------------------------------------------------------------------


def _slots(value) -> tuple[Slot, ...]:
    return tuple(
        Slot(str(cid), CATEGORY_LABELS.get(str(cid), str(cid))) for cid in _as_list(value)
    )


def describe_rule(rule: dict) -> RuleCard:
    """One rule as a card. Bands are paired later, by `describe`."""
    groups = tuple(
        ClauseGroup(
            kind=section,
            lead=LEADS[section],
            lines=tuple(describe_condition(c) for c in rule[section]),
        )
        for section in triage.CONDITION_SECTIONS
        if rule.get(section)
    )
    return RuleCard(
        id=str(rule.get("id", "?")),
        score=float(rule.get("score", 0)),
        note=str(rule.get("note", "") or ""),
        slots=_slots(rule.get("categories")),
        excluded=_slots(rule.get("exclude_categories")),
        groups=groups,
        source=rule,
    )


def _sole_none(card: RuleCard) -> dict | None:
    """The one `none` condition of a rule that has exactly one."""
    conds = card.source.get("none") or []
    if len(conds) != 1 or not isinstance(conds[0], dict):
        return None
    return conds[0]


def _comparable(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return str(value)


def _selector_of(cond: dict) -> tuple | None:
    """Which quantity a condition tests, with its bound left out.

    A scalar's value *is* its bound (`{ilvl: {min: 86}}`), so for those the key
    alone names the quantity; for every other family the value names it.
    """
    family = _family_of(cond)
    if family is None:
        return None
    if family in _SCALAR_FAMILIES:
        return (family, ())
    return (
        family,
        tuple((key, _comparable(cond[key])) for key in sorted(family & cond.keys())),
    )


def _min_of(cond: dict) -> float | None:
    family = _family_of(cond)
    if family is None:
        return None
    spec = cond[next(iter(family))] if family in _SCALAR_FAMILIES else cond
    if isinstance(spec, bool):
        return float(spec)
    if isinstance(spec, (int, float)):
        return float(spec)
    if isinstance(spec, dict) and "min" in spec:
        try:
            return float(spec["min"])
        except (TypeError, ValueError):
            return None
    return None


def _floor_index(card: RuleCard, boundary: dict) -> int | None:
    """Where in `all` this rule states the floor its `none` puts a ceiling on."""
    ceiling = _min_of(boundary)
    if ceiling is None:
        return None
    wanted = _selector_of(boundary)
    for i, cond in enumerate(card.source.get("all") or []):
        if not isinstance(cond, dict) or _selector_of(cond) != wanted:
            continue
        floor = _min_of(cond)
        if floor is not None and floor < ceiling:
            return i
    return None


def bands(cards: Sequence[RuleCard]) -> dict[str, BandNote]:
    """Rules written as disjoint bands, paired up, keyed by rule id.

    A rule is the lower band of a pair when it has exactly one `none` condition,
    some `all` condition of another rule is **equal to it as a dict**, and it
    also states a lower floor on the same selector. Exact dict equality is the
    whole safety of this: it recognises the two pairs the ruleset really has
    (`high-life`/`very-high-life`, `high-total-res`/`huge-total-res`) and cannot
    invent a third from rules that merely look similar.

    Detecting rules that *stack* is deliberately not attempted. The obvious
    heuristic - same slot, same selector, different `min` - yields six
    candidates here, four of them wrong, and its worst false positive would read
    "a +2 roll fires both, scoring 46" about `helm-socketed-gem-level` and
    `chest-socketed-gem-level`, where the higher threshold carries the *lower*
    score. Co-firing is measured from observations instead, not inferred.
    """
    notes: dict[str, BandNote] = {}
    for card in cards:
        boundary = _sole_none(card)
        if boundary is None or _floor_index(card, boundary) is None:
            continue
        partner = next(
            (
                other
                for other in cards
                if other.id != card.id
                and any(cond == boundary for cond in (other.source.get("all") or []))
            ),
            None,
        )
        if partner is None:
            continue
        ceiling = _num(_min_of(boundary))
        notes[card.id] = BandNote(
            partner_id=partner.id,
            lower=True,
            text=(
                f"The lower of two disjoint bands: anything at {ceiling} or more is "
                f"scored by `{partner.id}` instead, so the two can never both fire."
            ),
        )
        notes[partner.id] = BandNote(
            partner_id=card.id,
            lower=False,
            text=(
                f"The upper of two disjoint bands: `{card.id}` scores the rolls "
                f"below {ceiling}."
            ),
        )
    return notes


def _with_band(card: RuleCard, note: BandNote | None) -> RuleCard:
    """Attach a band note, folding a lower band's ceiling into its own headline.

    The `none` group goes away with it: read on its own, "excluded when effective
    life is 115 or more" is a rule that penalises rolling well, which is the
    opposite of what the pair means.
    """
    if note is None or not note.lower:
        return replace(card, band=note)
    boundary = _sole_none(card)
    index = _floor_index(card, boundary)
    ceiling = _num(_min_of(boundary))
    groups = []
    for group in card.groups:
        if group.kind == "none":
            continue
        if group.kind == "all":
            lines = list(group.lines)
            lines[index] = replace(
                lines[index], text=f"{lines[index].text}, but under {ceiling}"
            )
            group = replace(group, lines=tuple(lines))
        groups.append(group)
    return replace(card, groups=tuple(groups), band=note)


def sections_for(cards: Sequence[RuleCard]) -> tuple[SlotSection, ...]:
    """The rules grouped by the slots they apply to, empty sections dropped.

    `exclude_categories` never creates a section: `high-life` excludes jewels,
    and manufacturing a Jewel section to list a rule that cannot fire there
    would be worse than saying nothing. Order follows `CATEGORY_LABELS`, so
    adding a category orders itself.
    """
    sections: list[SlotSection] = []
    universal = _ranked(c for c in cards if c.universal)
    if universal:
        sections.append(
            SlotSection(slot=None, label=EVERY_SLOT, anchor="slot-every", rules=universal)
        )
    named = list(dict.fromkeys(s.id for c in cards for s in c.slots))
    ordered = [cid for cid in CATEGORY_LABELS if cid in named]
    # An id no category label knows is a `validate-rules` finding, not a reason
    # for the rule to vanish off the page.
    ordered += [cid for cid in named if cid not in CATEGORY_LABELS]
    for cid in ordered:
        slot = Slot(cid, CATEGORY_LABELS.get(cid, cid))
        sections.append(
            SlotSection(
                slot=slot,
                label=slot.label,
                anchor=slot.anchor,
                rules=_ranked(c for c in cards if any(s.id == cid for s in c.slots)),
            )
        )
    return tuple(sections)


def _ranked(cards: Iterable[RuleCard]) -> tuple[RuleCard, ...]:
    return tuple(sorted(cards, key=lambda c: (-c.score, c.id)))


# --------------------------------------------------------------------------
# The whole ruleset
# --------------------------------------------------------------------------


def _terms(labels: dict[str, Term]) -> tuple[TermDoc, ...]:
    return tuple(
        TermDoc(name=name, label=term.label, definition=term.definition, unit=term.unit)
        for name, term in labels.items()
    )


def _settings(ruleset: triage.Ruleset) -> SettingsDoc:
    promote, floor = _num(ruleset.promote_score), ruleset.min_ilvl
    return SettingsDoc(
        promote_score=ruleset.promote_score,
        min_ilvl=floor,
        max_market_checks=ruleset.max_market_checks,
        fingerprint=ruleset.fingerprint,
        order=(
            "Each rare is parsed and its pseudo totals computed. This is free "
            "and offline.",
            "The gates below reject the item outright, before any rule runs.",
            "The vetoes run next. One match discards the item, whatever it would "
            "have scored.",
            "Every remaining rule whose conditions hold adds its score. Scores "
            "are additive and rules may overlap.",
            f"An item reaching {promote} is promoted to a market check, up to "
            f"{ruleset.max_market_checks} checks per scan.",
        ),
        gates=(
            "not rare - magic, normal and unique items are never scored",
            "unidentified - its mods cannot be read",
            f"item level below {floor}"
            if floor
            else "no item level floor is set, so nothing is rejected for ilvl",
        ),
    )


# Observations per rule below which the row is an anecdote and says so. Not
# `trade.MIN_CONFIDENT_SAMPLE`, which happens to be the same number about a
# different quantity - priced listings inside one check, not checks per rule.
ANECDOTE_BELOW = 5


def price_tone(median: float | None, baseline: float | None) -> str:
    """A CSS class for a median, read against the overall base rate.

    The tone goes on the median and nothing else. `n`, `best` and the tail count
    are facts about sample size and what was caught; colouring them would imply a
    judgement that only a comparison against the baseline can support.
    """
    if median is None or not baseline:
        return ""
    if median >= baseline * calibration.GOOD_MULTIPLE:
        return "good"
    if median <= baseline:
        return "bad"
    return ""


def evidence_from(rows: Sequence[dict], ruleset: triage.Ruleset, *, pooled: bool = False) -> Evidence:
    """What the stored market checks say, per rule.

    Takes row dicts rather than a ``Cache`` - like every function in
    ``calibration`` - so the CLI owns the one line of I/O and this stays testable
    without a database.

    Observations labelled by another ruleset are **set aside** by default, and
    the page says how many: a rule id means nothing without the ruleset that
    defined it, and pooling two vocabularies produces a table that looks like
    evidence and is not. ``pooled`` opts into pooling them anyway.
    """
    considered = list(rows) if pooled else calibration.matching(list(rows), ruleset.fingerprint)
    priced = calibration.priced(considered)
    threshold = calibration.TAIL_THRESHOLD
    return Evidence(
        observations=len(rows),
        considered=len(considered),
        priced=len(priced),
        setaside=len(calibration.stale(list(rows), ruleset.fingerprint)),
        vocabularies=tuple(calibration.vocabularies(list(rows))),
        baseline=calibration.baseline(priced),
        # min_samples stays at 1 on purpose. On `analyse` it hides table rows;
        # here a row *is* a rule, so hiding one because its evidence is thin
        # would hide the page's subject. Thin rows are labelled, not dropped.
        by_rule={g.key: g for g in calibration.by_rule(priced, threshold)},
        never_fired=frozenset(calibration.never_fired(priced, ruleset)),
        # Counted over the same priced rows as every other number here, so the
        # pair counts and the per-rule counts are comparable.
        together=tuple(calibration.co_occurrence(priced)),
        threshold=threshold,
        pooled=pooled,
    )


def describe(ruleset: triage.Ruleset) -> RulesetDoc:
    """The whole ruleset as documentation. Reads nothing but the ruleset."""
    # A rule that is not a mapping cannot be scored against at all - `assess`
    # would raise on it - so it is skipped here as `ambiguous_conditions` skips
    # it, rather than being described as though it were a rule.
    veto = tuple(describe_rule(r) for r in ruleset.veto if isinstance(r, dict))
    rules = tuple(describe_rule(r) for r in ruleset.rules if isinstance(r, dict))
    notes = bands(rules)
    rules = tuple(_with_band(card, notes.get(card.id)) for card in rules)
    return RulesetDoc(
        settings=_settings(ruleset),
        veto=veto,
        rules=rules,
        sections=sections_for(rules),
        pseudos=_terms(_PSEUDO_LABELS),
        flags=_terms(_FLAG_LABELS),
        scalars=_terms(_SCALAR_LABELS),
    )
