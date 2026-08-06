"""Layer 1: free, offline scoring that decides which items deserve an API call.

Rate limits make one trade query per item impossible for a dump tab, so this
stage has to do the heavy lifting. It scores each item against a YAML ruleset
and only the survivors reach the market-verification stage.

A rule is a list of conditions plus a score. Conditions can test:

    pseudo:  a computed aggregate      {pseudo: life, min: 90}
    mod:     an exact mod template     {mod: "#% increased Attack Speed", min: 12}
    mod_any: any of several templates  {mod_any: [...], min: 20}
    stat:    a trade stat id           {stat: explicit.stat_124131830, min: 1}
    flag:    a boolean item property   {flag: influenced}
    base:    an exact base type        {base_any: [Opal Ring, Two-Stone Ring]}
    ilvl / links / sockets / quality   {ilvl: {min: 84}}

Base type is a much finer test than ``category``: an Opal Ring and an Iron Ring
are both ``accessory.ring``. Note that base value is conditional rather than
intrinsic - seven of the ten known-1c reference rings are ilvl 83-85 Two-Stone
Rings, a base meta knowledge calls desirable - so a ``base`` condition almost
always belongs alongside an ``ilvl`` or mod condition rather than scoring alone.

**One condition names one selector.** ``{base: Opal Ring, ilvl: {min: 84}}``
looks like a conjunction and is not one: ``evaluate_condition`` reads a single
selector and discards the rest, so that form returned True for an ilvl 86 Iron
Ring. Loading a ruleset that contains it now raises ``RulesetError`` instead;
write the two as separate entries under ``all``, where they really are ANDed.

Rejecting rather than teaching ``evaluate_condition`` to AND them is deliberate.
``min``/``max``/``abs`` are modifiers of whichever selector is present, so a
condition naming two selectors that both consume them (``{pseudo: life, mod: X,
min: 90}``) has no single defensible reading. ANDing would also change which
mods land in ``matched``, and ``matched`` is what the report cites as the reason
an item was flagged and what the trade query filters on - a behaviour change in
ranking-adjacent code, to support a form no rule uses. Rejecting is a no-op for
every ruleset that was ever correct.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .items import Item
from .pseudo import Pseudo, compute

RULES_DIR = Path(__file__).parent / "rules"
DEFAULT_RULES = RULES_DIR / "default.yaml"


@dataclass
class Hit:
    rule_id: str
    score: float
    note: str
    mods: list = field(default_factory=list)  # the Mod objects that satisfied it


@dataclass
class Verdict:
    item: Item
    pseudo: Pseudo
    score: float = 0.0
    hits: list[Hit] = field(default_factory=list)
    rejected: str | None = None

    @property
    def promoted(self) -> bool:
        return self.rejected is None and self.score >= self._threshold

    _threshold: float = 25.0

    @property
    def reasons(self) -> list[str]:
        return [h.note for h in sorted(self.hits, key=lambda h: -h.score) if h.note]


class Ruleset:
    """A loaded ruleset, rejected outright if it cannot be evaluated as written.

    The check happens here rather than only in ``validate-rules`` because
    ``validate-rules`` is a command the user has to remember to run: someone who
    edits the YAML and goes straight to ``scan`` would otherwise get scores
    computed from a condition half of which was discarded.
    """

    def __init__(self, data: dict) -> None:
        ambiguous = ambiguous_conditions(data)
        if ambiguous:
            raise RulesetError("\n".join(str(a) for a in ambiguous))

        self.settings = data.get("settings") or {}
        self.rules = data.get("rules") or []
        self.veto = data.get("veto") or []
        self.promote_score = float(self.settings.get("promote_score", 25))
        self.min_ilvl = int(self.settings.get("min_ilvl", 0))
        self.max_market_checks = int(self.settings.get("max_market_checks", 40))
        self.fingerprint = fingerprint(data)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Ruleset":
        p = Path(path) if path else DEFAULT_RULES
        return cls(yaml.safe_load(p.read_text()) or {})


def fingerprint(data: dict) -> str:
    """A short, stable hash of the parts of a ruleset that decide scoring.

    Recorded alongside every price observation, because a rule id on its own is
    not a stable label: rules get retuned, split and renamed, and the stored
    ``rules_hit`` then describes a ruleset that no longer exists while reading
    exactly like current evidence.

    That is not hypothetical. When the life rules were nested, an item over 115
    life hit ``high-life`` *and* ``very-high-life``; splitting them into disjoint
    bands made that impossible. 27 observations in the author's cache carry both
    ids, and ``analyse`` presented them as evidence about the split rules -
    reporting the two bands as firing on an identical 24 items at an identical
    median, which is the nested behaviour the split was meant to end.

    Covers ``rules``, ``veto`` and the two settings that decide whether an item
    is scored at all. ``max_market_checks`` is deliberately excluded: it caps
    how many items get priced, not how any item is judged, so changing it must
    not orphan the evidence collected so far.
    """
    material = {
        "rules": data.get("rules") or [],
        "veto": data.get("veto") or [],
        "promote_score": (data.get("settings") or {}).get("promote_score"),
        "min_ilvl": (data.get("settings") or {}).get("min_ilvl"),
    }
    blob = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


# --------------------------------------------------------------------------
# Condition evaluation
# --------------------------------------------------------------------------

_PSEUDO_FIELDS = {
    "life": lambda p: p.effective_life,
    "flat_life": lambda p: p.life,
    "increased_life": lambda p: p.increased_life,
    "energy_shield": lambda p: p.energy_shield,
    "increased_es": lambda p: p.increased_es,
    "mana": lambda p: p.mana,
    "elemental_resistance": lambda p: p.elemental_resistance,
    "total_resistance": lambda p: p.total_resistance,
    "chaos_resistance": lambda p: p.chaos_resistance,
    "count_resistances": lambda p: float(p.count_resistances),
    "count_elemental_resistances": lambda p: float(p.count_elemental_resistances),
    "attributes": lambda p: p.total_attributes,
}

_FLAGS = {
    "influenced": lambda it: bool(it.influences),
    "fractured": lambda it: it.fractured,
    "synthesised": lambda it: it.synthesised,
    "corrupted": lambda it: it.corrupted,
    "mirrored": lambda it: it.mirrored,
    "identified": lambda it: it.identified,
    "shaper": lambda it: "shaper" in it.influences,
    "elder": lambda it: "elder" in it.influences,
    "crusader": lambda it: "crusader" in it.influences,
    "hunter": lambda it: "hunter" in it.influences,
    "redeemer": lambda it: "redeemer" in it.influences,
    "warlord": lambda it: "warlord" in it.influences,
}

_SCALARS = {
    "ilvl": lambda it: float(it.ilvl),
    "links": lambda it: float(it.links),
    "sockets": lambda it: float(it.sockets),
    "quality": lambda it: float(it.quality),
    "influence_count": lambda it: float(len(it.influences)),
    "mod_count": lambda it: float(len(it.mods_in("explicit", "fractured"))),
}


# The selectors `evaluate_condition` below understands, grouped by the ones it
# reads together. Within a group the keys are unioned, so `{mod: A, mod_any:
# [B]}` tests A and B and is a legitimate condition. Across groups only one is
# read and the others are silently dropped, which is why naming two is rejected
# at load time - see `conflicting_selectors`.
SELECTOR_FAMILIES = (
    frozenset({"pseudo"}),
    frozenset({"mod", "mod_any"}),
    frozenset({"mod_matches"}),
    frozenset({"stat", "stat_any"}),
    frozenset({"flag"}),
    frozenset({"base", "base_any"}),
    frozenset({"base_matches"}),
    frozenset({"category"}),
    *(frozenset({name}) for name in _SCALARS),
)

# Every key `evaluate_condition` below understands. `validate-rules` checks the
# ruleset against these rather than keeping its own list, because the two did
# drift: `mod_matches` was added here and never added there, so the eleven
# highest-scoring rules in the ruleset went unvalidated. A condition key that
# is not in this set matches nothing and fails silently, forever.
CONDITION_VALUE_KEYS = frozenset().union(*SELECTOR_FAMILIES)
# Modifiers qualify whichever selector the condition names, so they co-occur
# with one freely: `{pseudo: attributes, min: 70}` is the normal form.
CONDITION_MODIFIER_KEYS = frozenset({"min", "max", "abs", "is"})
CONDITION_KEYS = CONDITION_VALUE_KEYS | CONDITION_MODIFIER_KEYS

FLAG_NAMES = frozenset(_FLAGS)
PSEUDO_NAMES = frozenset(_PSEUDO_FIELDS)


class RulesetError(ValueError):
    """A ruleset that cannot be evaluated as written.

    Subclasses ``ValueError`` so callers already guarding ``Ruleset.load``
    against a malformed file catch it too.
    """


@dataclass(frozen=True)
class AmbiguousCondition:
    """A condition naming several selectors, only one of which would be read."""

    rule_id: str
    section: str
    keys: tuple[str, ...]

    @property
    def where(self) -> str:
        return f"{self.section}: {', '.join(self.keys)}"

    @property
    def reason(self) -> str:
        return (
            "condition names more than one selector - only one of them is "
            "evaluated and the rest are silently ignored. Write one condition "
            "per selector; entries under `all` are ANDed."
        )

    def __str__(self) -> str:
        return f"rule '{self.rule_id}' ({self.where}) {self.reason}"


def conflicting_selectors(cond: dict) -> tuple[str, ...]:
    """The selector keys in ``cond`` that cannot be evaluated together.

    Empty for every condition ``evaluate_condition`` reads in full: one
    selector, optionally with ``min``/``max``/``abs``/``is``, and optionally
    with its own family's aliases (``mod`` beside ``mod_any``).
    """
    if not isinstance(cond, dict):
        return ()
    named = [family & cond.keys() for family in SELECTOR_FAMILIES]
    named = [keys for keys in named if keys]
    if len(named) < 2:
        return ()
    return tuple(sorted(key for keys in named for key in keys))


def ambiguous_conditions(data: dict) -> list[AmbiguousCondition]:
    """Every condition in a raw ruleset that names more than one selector.

    Returned rather than raised so ``validate-rules`` can list them alongside
    its other findings instead of dying on the first one.
    """
    found: list[AmbiguousCondition] = []
    for section in ("veto", "rules"):
        for rule in data.get(section) or []:
            if not isinstance(rule, dict):
                continue
            rule_id = str(rule.get("id", "?"))
            for group in ("all", "any", "none"):
                for cond in rule.get(group) or []:
                    keys = conflicting_selectors(cond)
                    if keys:
                        found.append(AmbiguousCondition(rule_id, group, keys))
    return found


def _in_range(value: float, spec) -> bool:
    if isinstance(spec, (int, float)):
        return value >= float(spec)
    if not isinstance(spec, dict):
        return False
    if "min" in spec and value < float(spec["min"]):
        return False
    if "max" in spec and value > float(spec["max"]):
        return False
    return True


def _best_mod(item: Item, predicate, use_abs: bool = False) -> "tuple[float, object] | None":
    """The highest-magnitude mod satisfying ``predicate``, with its value.

    ``use_abs`` compares on absolute value, for mods whose worth grows as the
    roll goes more negative (``-7 to Total Mana Cost of Skills``).
    """
    best = None
    for mod in item.mods:
        if not predicate(mod):
            continue
        value = abs(mod.value) if use_abs else mod.value
        if best is None or value > best[0]:
            best = (value, mod)
    return best


def _as_list(v) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def evaluate_condition(cond: dict, item: Item, p: Pseudo, matched: list | None = None) -> bool:
    """Test one condition. Satisfying mods are appended to ``matched``.

    Reads exactly one selector - the first present, in the order below - so a
    condition naming two is refused when the ruleset loads rather than half
    evaluated here. See ``conflicting_selectors``.
    """
    if not isinstance(cond, dict):
        return False

    use_abs = bool(cond.get("abs", False))

    def hit(found) -> bool:
        bounded = "min" in cond or "max" in cond
        if found is None:
            return False
        value, mod = found
        if bounded and not _in_range(value, cond):
            return False
        if matched is not None:
            matched.append(mod)
        return True

    if "pseudo" in cond:
        getter = _PSEUDO_FIELDS.get(str(cond["pseudo"]))
        if not getter:
            return False
        return _in_range(getter(p), cond)

    if "mod" in cond or "mod_any" in cond:
        wanted = set(_as_list(cond.get("mod")) + _as_list(cond.get("mod_any")))
        return hit(_best_mod(item, lambda m: m.template in wanted, use_abs))

    if "mod_matches" in cond:
        # Escape hatch for families of mods. Tested against both the rolled
        # text and the template, so patterns can be written either way -
        # "#% increased Spell Damage" and "9\\d% increased Spell Damage" both work.
        pattern = re.compile(str(cond["mod_matches"]), re.IGNORECASE)
        return hit(
            _best_mod(
                item,
                lambda m: bool(pattern.search(m.text) or pattern.search(m.template)),
                use_abs,
            )
        )

    if "stat" in cond or "stat_any" in cond:
        ids = set(_as_list(cond.get("stat")) + _as_list(cond.get("stat_any")))
        return hit(_best_mod(item, lambda m: m.stat_id in ids, use_abs))

    if "flag" in cond:
        getter = _FLAGS.get(str(cond["flag"]))
        want = bool(cond.get("is", True))
        return bool(getter and getter(item)) == want

    for key, getter in _SCALARS.items():
        if key in cond:
            return _in_range(getter(item), cond[key])

    if "base" in cond or "base_any" in cond:
        # Exact, case-insensitive - base type names are fixed strings from the
        # game data, so a substring match would silently conflate "Ruby Ring"
        # with "Ruby Flask". Use base_matches for families.
        wanted = {b.strip().lower() for b in _as_list(cond.get("base")) + _as_list(cond.get("base_any"))}
        return item.base_type.strip().lower() in wanted

    if "base_matches" in cond:
        return bool(re.search(str(cond["base_matches"]), item.base_type, re.IGNORECASE))

    if "category" in cond:
        return item.category in _as_list(cond["category"])

    return False


def _rule_applies(rule: dict, item: Item) -> bool:
    cats = _as_list(rule.get("categories"))
    if cats and item.category not in cats:
        return False
    excl = _as_list(rule.get("exclude_categories"))
    if excl and item.category in excl:
        return False
    return True


def _conditions_hold(rule: dict, item: Item, p: Pseudo) -> "tuple[bool, list]":
    all_conds = rule.get("all") or []
    any_conds = rule.get("any") or []
    none_conds = rule.get("none") or []
    matched: list = []

    # `all` is evaluated without short-circuiting so every satisfying mod is
    # collected for the trade query, not just the ones before the first failure.
    if all_conds and not all([evaluate_condition(c, item, p, matched) for c in all_conds]):
        return False, []
    if any_conds and not any([evaluate_condition(c, item, p, matched) for c in any_conds]):
        return False, []
    if none_conds and any(evaluate_condition(c, item, p) for c in none_conds):
        return False, []
    return bool(all_conds or any_conds or none_conds), matched


def assess(item: Item, ruleset: Ruleset) -> Verdict:
    p = compute(item)
    v = Verdict(item=item, pseudo=p)
    v._threshold = ruleset.promote_score

    if item.rarity != "rare":
        v.rejected = "not rare"
        return v
    if not item.identified:
        v.rejected = "unidentified"
        return v
    if ruleset.min_ilvl and item.ilvl < ruleset.min_ilvl:
        v.rejected = f"ilvl {item.ilvl} below floor {ruleset.min_ilvl}"
        return v

    for rule in ruleset.veto:
        if _rule_applies(rule, item) and _conditions_hold(rule, item, p)[0]:
            v.rejected = str(rule.get("note") or rule.get("id") or "vetoed")
            return v

    for rule in ruleset.rules:
        if not _rule_applies(rule, item):
            continue
        held, matched = _conditions_hold(rule, item, p)
        if held:
            score = float(rule.get("score", 0))
            v.score += score
            v.hits.append(
                Hit(str(rule.get("id", "?")), score, str(rule.get("note", "")), matched)
            )

    return v


def notable_mods(verdict: Verdict, limit: int = 4) -> list:
    """Distinct mods that earned score, best-scoring rule first.

    These are what a buyer would actually search for, so they become the stat
    filters in the market-verification query.
    """
    out: list = []
    seen: set[int] = set()
    for h in sorted(verdict.hits, key=lambda h: -h.score):
        for m in h.mods:
            if id(m) not in seen and m.stat_id:
                seen.add(id(m))
                out.append(m)
            if len(out) >= limit:
                return out
    return out


def triage(items: list[Item], ruleset: Ruleset) -> list[Verdict]:
    """Score every item; returns verdicts sorted best-first."""
    verdicts = [assess(i, ruleset) for i in items]
    verdicts.sort(key=lambda v: (-v.score, v.item.label))
    return verdicts
