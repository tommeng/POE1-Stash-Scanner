"""Pseudo-stat aggregation, mirroring what the trade site's pseudo filters do.

Rares are priced on totals, not individual lines: a ring is "90 life, 75 res",
never "+90 life, +30 fire, +45 cold". These aggregates are both the input to
triage rules and the filters we send to the trade API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .items import Item

ELEMENTS = ("Fire", "Cold", "Lightning")

# ``+#% to Fire and Cold Resistances`` / ``+#% to all Elemental Resistances``
_RES_RE = re.compile(r"^\+#% to ([A-Za-z ]+?) Resistances?$")
# ``+# to Strength`` / ``+# to all Attributes`` / ``+# to Strength and Dexterity``
_ATTR_RE = re.compile(r"^\+# to ([A-Za-z ]+?)$")

_ATTR_NAMES = {"strength": "str", "dexterity": "dex", "intelligence": "int"}


def _split_terms(phrase: str) -> list[str]:
    parts = re.split(r"\s+and\s+", phrase.strip())
    out: list[str] = []
    for p in parts:
        p = p.strip()
        low = p.lower()
        if low in ("all elemental", "elemental"):
            out.extend(ELEMENTS)
        elif low == "all":
            out.extend([*ELEMENTS, "Chaos"])
        else:
            out.append(p)
    return out


@dataclass
class Pseudo:
    life: float = 0.0
    energy_shield: float = 0.0
    mana: float = 0.0
    increased_life: float = 0.0
    increased_es: float = 0.0
    resistances: dict[str, float] = field(default_factory=dict)
    attributes: dict[str, float] = field(default_factory=dict)

    @property
    def elemental_resistance(self) -> float:
        return sum(self.resistances.get(e, 0.0) for e in ELEMENTS)

    @property
    def total_resistance(self) -> float:
        return self.elemental_resistance + self.resistances.get("Chaos", 0.0)

    @property
    def chaos_resistance(self) -> float:
        return self.resistances.get("Chaos", 0.0)

    @property
    def count_elemental_resistances(self) -> int:
        return sum(1 for e in ELEMENTS if self.resistances.get(e, 0.0) > 0)

    @property
    def count_resistances(self) -> int:
        return sum(1 for v in self.resistances.values() if v > 0)

    @property
    def total_attributes(self) -> float:
        return sum(self.attributes.values())

    @property
    def effective_life(self) -> float:
        """Flat life including the half-life granted per point of Strength."""
        return self.life + self.attributes.get("str", 0.0) / 2.0

    def as_filters(self) -> dict[str, float]:
        """The subset worth sending to the trade API as pseudo stat filters."""
        out: dict[str, float] = {}
        if self.effective_life >= 1:
            out["pseudo.pseudo_total_life"] = round(self.effective_life)
        if self.energy_shield >= 1:
            out["pseudo.pseudo_total_energy_shield"] = round(self.energy_shield)
        if self.elemental_resistance >= 1:
            out["pseudo.pseudo_total_elemental_resistance"] = round(self.elemental_resistance)
        if self.total_resistance >= 1:
            out["pseudo.pseudo_total_resistance"] = round(self.total_resistance)
        if self.total_attributes >= 1:
            out["pseudo.pseudo_total_all_attributes"] = round(self.total_attributes)
        return out


def compute(item: Item) -> Pseudo:
    """Aggregate an item's mods into pseudo totals."""
    p = Pseudo()
    # Enchants and veiled mods do not contribute real stats to totals.
    for mod in item.mods_in("explicit", "implicit", "crafted", "fractured"):
        tpl = mod.template
        v = mod.value

        if tpl == "+# to maximum Life":
            p.life += v
            continue
        if tpl == "#% increased maximum Life":
            p.increased_life += v
            continue
        if tpl == "+# to maximum Energy Shield":
            p.energy_shield += v
            continue
        if tpl == "#% increased maximum Energy Shield":
            p.increased_es += v
            continue
        if tpl == "+# to maximum Mana":
            p.mana += v
            continue

        m = _RES_RE.match(tpl)
        if m:
            for term in _split_terms(m.group(1)):
                if term in ELEMENTS or term == "Chaos":
                    p.resistances[term] = p.resistances.get(term, 0.0) + v
            continue

        m = _ATTR_RE.match(tpl)
        if m:
            phrase = m.group(1).strip().lower()
            if phrase in ("all attributes", "all attribute"):
                for k in ("str", "dex", "int"):
                    p.attributes[k] = p.attributes.get(k, 0.0) + v
                continue
            terms = re.split(r"\s+and\s+", phrase)
            keys = [_ATTR_NAMES[t.strip()] for t in terms if t.strip() in _ATTR_NAMES]
            if keys and len(keys) == len(terms):
                for k in keys:
                    p.attributes[k] = p.attributes.get(k, 0.0) + v
            continue

    return p
