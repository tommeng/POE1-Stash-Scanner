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
    ilvl / links / sockets / quality   {ilvl: {min: 84}}
"""

from __future__ import annotations

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
    def __init__(self, data: dict) -> None:
        self.settings = data.get("settings") or {}
        self.rules = data.get("rules") or []
        self.veto = data.get("veto") or []
        self.promote_score = float(self.settings.get("promote_score", 25))
        self.min_ilvl = int(self.settings.get("min_ilvl", 0))
        self.max_market_checks = int(self.settings.get("max_market_checks", 40))

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Ruleset":
        p = Path(path) if path else DEFAULT_RULES
        return cls(yaml.safe_load(p.read_text()) or {})


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
    """Test one condition. Satisfying mods are appended to ``matched``."""
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
