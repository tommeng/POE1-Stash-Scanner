"""Synthetic stash-shaped items for testing.

Field names and shapes mirror what the stash API actually returns, so these
exercise the same parsing path as live data.
"""

from __future__ import annotations

RING_ICON = (
    "https://web.poecdn.com/gen/image/WzI1LDE0LHsiZiI6IjJESXRlbXMvUmluZ3MvU2FwcGhpcmVSdWJ5"
    "IiwidyI6MSwiaCI6MSwic2NhbGUiOjF9XQ/7df6d65dfd/SapphireRuby.png"
)
BOOT_ICON = (
    "https://web.poecdn.com/gen/image/WzI1LDE0LHsiZiI6IjJESXRlbXMvQXJtb3Vycy9Cb290cy9Cb290"
    "czEiLCJ3IjoyLCJoIjoyLCJzY2FsZSI6MX1d/abc/Boots1.png"
)
WAND_ICON = (
    "https://web.poecdn.com/gen/image/WzI1LDE0LHsiZiI6IjJESXRlbXMvV2VhcG9ucy9PbmVIYW5kV2Vh"
    "cG9ucy9XYW5kcy9XYW5kMSIsInciOjEsImgiOjMsInNjYWxlIjoxfV0/def/Wand1.png"
)
HELM_ICON = (
    "https://web.poecdn.com/gen/image/WzI1LDE0LHsiZiI6IjJESXRlbXMvQXJtb3Vycy9IZWxtZXRzL0hl"
    "bG1ldEludDEiLCJ3IjoyLCJoIjoyLCJzY2FsZSI6MX1d/ghi/HelmetInt1.png"
)


def item(**kw) -> dict:
    """A rare item with sensible defaults, overridable per test."""
    base = {
        "id": kw.pop("id", "test-item"),
        "name": kw.pop("name", "Test Item"),
        "baseType": kw.pop("baseType", "Two-Stone Ring"),
        "typeLine": kw.pop("typeLine", None) or kw.get("baseType", "Two-Stone Ring"),
        "icon": kw.pop("icon", RING_ICON),
        "ilvl": kw.pop("ilvl", 84),
        "rarity": kw.pop("rarity", "Rare"),
        "identified": kw.pop("identified", True),
        "frameType": 2,
        "x": kw.pop("x", 0),
        "y": kw.pop("y", 0),
        "w": 1,
        "h": 1,
    }
    base.update(kw)
    return base


# -- items that should NOT be flagged ---------------------------------------

ORDINARY_RING = item(
    id="ordinary-ring",
    name="Plague Loop",
    explicitMods=[
        "+111 to maximum Life",
        "36% increased Mana Regeneration Rate",
        "+12% to all Elemental Resistances",
        "+41% to Cold Resistance",
    ],
    implicitMods=["+15% to Fire and Cold Resistances"],
)

MID_ATTACK_RING = item(
    id="mid-attack-ring",
    name="Horror Turn",
    explicitMods=[
        "Adds 5 to 11 Physical Damage to Attacks",
        "+106 to maximum Life",
        "+9% to all Elemental Resistances",
        "+44% to Fire Resistance",
    ],
    implicitMods=["+15% to Fire and Lightning Resistances"],
)

JUNK_RING = item(
    id="junk-ring",
    name="Woe Coil",
    explicitMods=["+42 to maximum Life", "+18% to Fire Resistance", "+21 to Dexterity"],
    ilvl=72,
)

LOW_ILVL_RING = item(
    id="low-ilvl-ring", name="Weak Band", ilvl=68,
    explicitMods=["+90 to maximum Life", "+40% to Fire Resistance", "+40% to Cold Resistance"],
)

UNIDENTIFIED = item(id="unid", identified=False, explicitMods=[])


# -- items that SHOULD be flagged -------------------------------------------

TAILWIND_BOOTS = item(
    id="tailwind-boots",
    name="Hunter's Stride",
    baseType="Two-Toned Boots",
    icon=BOOT_ICON,
    ilvl=86,
    influences={"hunter": True},
    explicitMods=[
        "You have Tailwind if you have dealt a Critical Strike Recently",
        "35% increased Movement Speed",
        "+94 to maximum Life",
        "+41% to Cold Resistance",
    ],
)

PLUS_ONE_WAND = item(
    id="plus-one-wand",
    name="Storm Bite",
    baseType="Profane Wand",
    icon=WAND_ICON,
    ilvl=85,
    explicitMods=[
        "+1 to Level of all Spell Skill Gems",
        "94% increased Spell Damage",
        "+28% to Global Critical Strike Multiplier",
        "18% increased Cast Speed",
    ],
)

MINUS_RES_HELM = item(
    id="minus-res-helm",
    name="Doom Guard",
    baseType="Hubris Circlet",
    icon=HELM_ICON,
    ilvl=86,
    influences={"hunter": True},
    explicitMods=[
        "Nearby Enemies have -9% to Fire Resistance",
        "+99 to maximum Life",
        "+38% to Lightning Resistance",
    ],
)

MANA_COST_RING = item(
    id="mana-cost-ring",
    name="Empower Loop",
    ilvl=86,
    explicitMods=[
        "-7 to Total Mana Cost of Skills",
        "+92 to maximum Life",
        "+35% to Cold Resistance",
        "+30% to Lightning Resistance",
    ],
)

FRACTURED_RING = item(
    id="fractured-ring",
    name="Craft Base",
    ilvl=86,
    fractured=True,
    fracturedMods=["+105 to maximum Life"],
    explicitMods=["+35% to Fire Resistance", "+28 to Strength"],
)

CHAOS_RES_AMULET = item(
    id="chaos-amulet",
    name="Blight Heart",
    baseType="Marble Amulet",
    icon="https://web.poecdn.com/gen/image/WzI1LDE0LHsiZiI6IjJESXRlbXMvQW11bGV0cy9BbXVsZXQ1IiwidyI6MSwiaCI6MSwic2NhbGUiOjF9XQ/x/Amulet5.png",
    ilvl=86,
    explicitMods=[
        "+96 to maximum Life",
        "+31% to Chaos Resistance",
        "+40% to Fire Resistance",
        "+38% to Cold Resistance",
        "+35% to Lightning Resistance",
    ],
)


SHOULD_REJECT = [ORDINARY_RING, MID_ATTACK_RING, JUNK_RING, LOW_ILVL_RING, UNIDENTIFIED]
SHOULD_FLAG = [
    TAILWIND_BOOTS,
    PLUS_ONE_WAND,
    MINUS_RES_HELM,
    MANA_COST_RING,
    FRACTURED_RING,
    CHAOS_RES_AMULET,
]
