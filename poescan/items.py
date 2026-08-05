"""Normalised item model built from raw stash JSON."""

from __future__ import annotations

import base64
import json
import urllib.parse
from dataclasses import dataclass, field

from .tradedata import StatIndex, normalise, values_in

# Ordered art-path fragment -> trade category. The icon URL embeds the game's
# own asset path (``2DItems/Rings/SapphireRuby``), which is a far more reliable
# class signal than base-type name matching. Order matters: the first hit wins,
# so more specific paths must precede their parents.
_ART_CATEGORIES: list[tuple[str, str]] = [
    ("weapons/onehandweapons/wands", "weapon.wand"),
    ("weapons/onehandweapons/daggers/rune", "weapon.runedagger"),
    ("weapons/onehandweapons/daggers", "weapon.dagger"),
    ("weapons/onehandweapons/claws", "weapon.claw"),
    ("weapons/onehandweapons/onehandaxes", "weapon.oneaxe"),
    ("weapons/onehandweapons/onehandswords", "weapon.onesword"),
    ("weapons/onehandweapons/rapiers", "weapon.onesword"),
    ("weapons/onehandweapons/onehandmaces/sceptres", "weapon.sceptre"),
    ("weapons/onehandweapons/sceptres", "weapon.sceptre"),
    ("weapons/onehandweapons/onehandmaces", "weapon.onemace"),
    ("weapons/twohandweapons/bows", "weapon.bow"),
    ("weapons/twohandweapons/staves/warstaves", "weapon.warstaff"),
    ("weapons/twohandweapons/staves", "weapon.staff"),
    ("weapons/twohandweapons/warstaves", "weapon.warstaff"),
    ("weapons/twohandweapons/twohandaxes", "weapon.twoaxe"),
    ("weapons/twohandweapons/twohandswords", "weapon.twosword"),
    ("weapons/twohandweapons/twohandmaces", "weapon.twomace"),
    ("weapons/twohandweapons/fishingrods", "weapon.rod"),
    ("armours/bodyarmours", "armour.chest"),
    ("armours/boots", "armour.boots"),
    ("armours/gloves", "armour.gloves"),
    ("armours/helmets", "armour.helmet"),
    ("armours/shields", "armour.shield"),
    ("armours/quivers", "armour.quiver"),
    ("rings", "accessory.ring"),
    ("amulets/talismans", "accessory.amulet"),
    ("amulets", "accessory.amulet"),
    ("belts", "accessory.belt"),
    ("jewels/clusterjewels", "jewel.cluster"),
    ("jewels/abyss", "jewel.abyss"),
    ("jewels", "jewel.base"),
    ("quivers", "armour.quiver"),
    ("flasks", "flask"),
]

# Fallback when the icon is unhelpful: base-type name suffixes.
_NAME_CATEGORIES: list[tuple[str, str]] = [
    ("ring", "accessory.ring"),
    ("amulet", "accessory.amulet"),
    ("talisman", "accessory.amulet"),
    ("belt", "accessory.belt"),
    ("sash", "accessory.belt"),
    ("quiver", "armour.quiver"),
    ("wand", "weapon.wand"),
    ("sceptre", "weapon.sceptre"),
    ("bow", "weapon.bow"),
]

INFLUENCE_NAMES = ("shaper", "elder", "crusader", "hunter", "redeemer", "warlord", "searing", "tangled")

CATEGORY_LABELS = {
    "accessory.ring": "Ring",
    "accessory.amulet": "Amulet",
    "accessory.belt": "Belt",
    "armour.chest": "Body Armour",
    "armour.boots": "Boots",
    "armour.gloves": "Gloves",
    "armour.helmet": "Helmet",
    "armour.shield": "Shield",
    "armour.quiver": "Quiver",
    "weapon.wand": "Wand",
    "weapon.sceptre": "Sceptre",
    "weapon.dagger": "Dagger",
    "weapon.runedagger": "Rune Dagger",
    "weapon.claw": "Claw",
    "weapon.oneaxe": "One-Handed Axe",
    "weapon.onesword": "One-Handed Sword",
    "weapon.onemace": "One-Handed Mace",
    "weapon.bow": "Bow",
    "weapon.staff": "Staff",
    "weapon.warstaff": "Warstaff",
    "weapon.twoaxe": "Two-Handed Axe",
    "weapon.twosword": "Two-Handed Sword",
    "weapon.twomace": "Two-Handed Mace",
    "jewel.base": "Jewel",
    "jewel.abyss": "Abyss Jewel",
    "jewel.cluster": "Cluster Jewel",
    "flask": "Flask",
}


def art_path(icon_url: str) -> str:
    """Decode the base64 payload embedded in a poecdn icon URL to its art path."""
    if not icon_url:
        return ""
    try:
        segment = urllib.parse.urlparse(icon_url).path.split("/gen/image/")[-1].split("/")[0]
        padded = segment + "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8", "replace"))
        # Payload looks like [w, h, {"f": "2DItems/Rings/SapphireRuby", ...}]
        for part in payload:
            if isinstance(part, dict) and "f" in part:
                return str(part["f"])
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        pass
    # Older icons are plain paths.
    return urllib.parse.urlparse(icon_url).path


def classify(icon: str, base_type: str) -> str:
    path = art_path(icon).lower().replace("\\", "/")
    for fragment, category in _ART_CATEGORIES:
        if fragment in path:
            return category
    name = (base_type or "").lower()
    for suffix, category in _NAME_CATEGORIES:
        if name.endswith(suffix):
            return category
    return "unknown"


@dataclass
class Mod:
    text: str
    domain: str  # explicit | implicit | crafted | fractured | enchant | veiled
    stat_id: str | None = None
    values: list[float] = field(default_factory=list)

    @property
    def value(self) -> float:
        """Representative magnitude - the mean for 'adds X to Y' style rolls."""
        return sum(self.values) / len(self.values) if self.values else 0.0

    @property
    def template(self) -> str:
        return normalise(self.text)


@dataclass
class Item:
    id: str
    name: str
    base_type: str
    type_line: str
    category: str
    ilvl: int
    rarity: str
    identified: bool = True
    corrupted: bool = False
    mirrored: bool = False
    influences: list[str] = field(default_factory=list)
    fractured: bool = False
    synthesised: bool = False
    sockets: int = 0
    links: int = 0
    quality: int = 0
    mods: list[Mod] = field(default_factory=list)
    tab_name: str = ""
    tab_id: str = ""
    x: int = 0
    y: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        if self.name and self.name != self.base_type:
            return f"{self.name} {self.base_type}".strip()
        return self.type_line or self.base_type

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def position(self) -> str:
        # In-game coordinates are 1-indexed from the top-left.
        return f"col {self.x + 1}, row {self.y + 1}"

    def mods_in(self, *domains: str) -> list[Mod]:
        return [m for m in self.mods if m.domain in domains]

    def stat_value(self, stat_id: str) -> float | None:
        for m in self.mods:
            if m.stat_id == stat_id:
                return m.value
        return None


def _socket_info(raw: dict) -> tuple[int, int]:
    sockets = raw.get("sockets") or []
    if not sockets:
        return 0, 0
    groups: dict[int, int] = {}
    for s in sockets:
        g = s.get("group", 0)
        groups[g] = groups.get(g, 0) + 1
    return len(sockets), max(groups.values()) if groups else 0


def _quality(raw: dict) -> int:
    for prop in raw.get("properties") or []:
        if prop.get("name") == "Quality":
            vals = prop.get("values") or []
            if vals and vals[0]:
                nums = values_in(str(vals[0][0]))
                if nums:
                    return int(nums[0])
    return 0


def _mod_text(entry) -> tuple[str, dict]:
    """Mod entries are either plain strings or ``{description, flags}`` objects."""
    if isinstance(entry, str):
        return entry, {}
    if isinstance(entry, dict):
        return str(entry.get("description", "")), entry.get("flags") or {}
    return str(entry), {}


_MOD_FIELDS = [
    ("implicitMods", "implicit"),
    ("explicitMods", "explicit"),
    ("craftedMods", "crafted"),
    ("fracturedMods", "fractured"),
    ("enchantMods", "enchant"),
    ("veiledMods", "veiled"),
    ("scourgeMods", "scourge"),
]


def from_stash_json(raw: dict, index: StatIndex, tab_name: str = "", tab_id: str = "") -> Item:
    """Build an Item from one entry of a stash tab's ``items`` array."""
    icon = raw.get("icon", "")
    base_type = raw.get("baseType") or raw.get("typeLine") or ""
    # Current responses carry influences as an object, but also duplicate them
    # as top-level booleans ("shaper": true). Accept either, so an older or
    # legacy-shaped payload does not silently lose a 14-point signal.
    influences = set((raw.get("influences") or {}).keys())
    influences |= {name for name in INFLUENCE_NAMES if raw.get(name) is True}
    influences = sorted(influences)

    mods: list[Mod] = []
    seen_hashes = (raw.get("extended") or {}).get("hashes") or {}

    for key, domain in _MOD_FIELDS:
        for i, entry in enumerate(raw.get(key) or []):
            text, flags = _mod_text(entry)
            if not text:
                continue
            effective = domain
            if flags.get("crafted"):
                effective = "crafted"
            elif flags.get("fractured"):
                effective = "fractured"
            values = values_in(text)
            stat_id = _hash_for(seen_hashes, domain, i)
            if stat_id is None:
                stat_id, sign = index.resolve(text, effective)
                # "32% reduced X" is "-32% increased X" in trade terms; keep the
                # value in the trade site's frame so comparisons stay coherent.
                if sign < 0:
                    values = [-v for v in values]
            mods.append(Mod(text=text, domain=effective, stat_id=stat_id, values=values))

    sockets, links = _socket_info(raw)
    return Item(
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        base_type=base_type,
        type_line=raw.get("typeLine", base_type),
        category=classify(icon, base_type),
        ilvl=int(raw.get("ilvl") or 0),
        rarity=(raw.get("rarity") or "").lower() or _rarity_from_frame(raw.get("frameType")),
        identified=bool(raw.get("identified", True)),
        corrupted=bool(raw.get("corrupted", False)),
        mirrored=bool(raw.get("mirrored", False)),
        influences=influences,
        fractured=bool(raw.get("fractured", False)) or any(m.domain == "fractured" for m in mods),
        synthesised=bool(raw.get("synthesised", False)),
        sockets=sockets,
        links=links,
        quality=_quality(raw),
        mods=mods,
        tab_name=tab_name,
        tab_id=tab_id,
        x=int(raw.get("x") or 0),
        y=int(raw.get("y") or 0),
        raw=raw,
    )


def _hash_for(hashes: dict, domain: str, position: int) -> str | None:
    """Use GGG's own stat hashes when the payload provides them."""
    entries = hashes.get(domain)
    if not entries or position >= len(entries):
        return None
    entry = entries[position]
    if isinstance(entry, (list, tuple)) and entry:
        return str(entry[0])
    return None


_FRAME_RARITY = {0: "normal", 1: "magic", 2: "rare", 3: "unique", 4: "gem", 5: "currency", 6: "divination"}


def _rarity_from_frame(frame) -> str:
    try:
        return _FRAME_RARITY.get(int(frame), "normal")
    except (TypeError, ValueError):
        return "normal"
