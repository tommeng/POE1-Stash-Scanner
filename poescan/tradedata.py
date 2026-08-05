"""Cached copies of GGG's public trade metadata (stats, filters, leagues).

These endpoints need no authentication. They change with each league/patch, so
we cache to disk with a TTL rather than vendoring them.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from . import USER_AGENT
from .config import DATA_DIR, TRADE_BASE, ensure_dirs

TTL_SECONDS = 7 * 24 * 3600

_ENDPOINTS = {
    "stats": "/api/trade/data/stats",
    "items": "/api/trade/data/items",
    "filters": "/api/trade/data/filters",
    "static": "/api/trade/data/static",
    "leagues": "/api/trade/data/leagues",
}


def _cache_path(name: str) -> Path:
    return DATA_DIR / f"{name}.json"


def fetch(name: str, force: bool = False) -> dict:
    """Return trade metadata, downloading it if missing or stale."""
    if name not in _ENDPOINTS:
        raise KeyError(f"Unknown trade data set: {name}")
    ensure_dirs()
    path = _cache_path(name)
    if not force and path.exists() and (time.time() - path.stat().st_mtime) < TTL_SECONDS:
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass  # fall through and re-download

    resp = httpx.get(
        TRADE_BASE + _ENDPOINTS[name],
        headers={"User-Agent": USER_AGENT},
        timeout=60.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    data = resp.json()
    path.write_text(json.dumps(data))
    return data


# --------------------------------------------------------------------------
# Mod text -> trade stat id
# --------------------------------------------------------------------------

_NUM = re.compile(r"[+-]?\d+(?:\.\d+)?")


def normalise(text: str) -> str:
    """Turn a rolled mod line into the trade API's template form.

    ``"+97 to maximum Life"`` -> ``"+# to maximum Life"``

    Numbers become ``#``. A sign immediately preceding a number is preserved,
    because the trade templates distinguish ``+#`` from ``#``.
    """
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+", " ", text)

    def repl(m: re.Match) -> str:
        tok = m.group(0)
        return ("+" if tok.startswith("+") else "-" if tok.startswith("-") else "") + "#"

    return _NUM.sub(repl, text)


def _signless(template: str) -> str:
    return template.replace("+#", "#").replace("-#", "#")


# The game and the trade site word the same mod differently in three
# systematic ways. Each generator below produces a candidate template plus the
# sign the rolled value needs to keep its meaning.
_INVERSIONS = ((r"\breduced\b", "increased"), (r"\bless\b", "more"))
_ARTICLE = re.compile(r"\b(?:an|a)\b")


def _invert(template: str) -> str | None:
    """``#% reduced X`` -> ``#% increased X`` (value must then be negated)."""
    out = template
    for pattern, replacement in _INVERSIONS:
        out = re.sub(pattern, replacement, out)
    return out if out != template else None


def _pluralise_last_word(template: str) -> str:
    words = template.rsplit(" ", 1)
    if len(words) != 2 or words[1].endswith("s"):
        return template
    return f"{words[0]} {words[1]}s"


def _candidates(text: str) -> "list[tuple[str, float]]":
    """Every (template, sign) worth trying, best first."""
    tpl = normalise(text)
    out: list[tuple[str, float]] = [(tpl, 1.0)]

    # Local defence mods carry a "(Local)" suffix on the trade site only.
    out.append((f"{tpl} (Local)", 1.0))

    inverted = _invert(tpl)
    if inverted:
        out.append((inverted, -1.0))
        out.append((f"{inverted} (Local)", -1.0))

    # "You can apply an additional Curse" -> "You can apply # additional Curses"
    if _ARTICLE.search(tpl):
        counted = _ARTICLE.sub("#", tpl, count=1)
        out.append((counted, 1.0))
        out.append((_pluralise_last_word(counted), 1.0))

    seen: set[str] = set()
    return [(c, s) for c, s in out if not (c in seen or seen.add(c))]


class StatIndex:
    """Lookup from a rolled mod line to its trade stat filter id."""

    # Which trade stat groups to consult for each kind of mod on an item.
    DOMAIN_GROUPS = {
        "explicit": ("Explicit", "Pseudo"),
        "implicit": ("Implicit",),
        "crafted": ("Crafted", "Explicit"),
        "fractured": ("Fractured", "Explicit"),
        "enchant": ("Enchant",),
        "veiled": ("Veiled",),
        "scourge": ("Scourge",),
    }

    def __init__(self, stats_payload: dict) -> None:
        self.by_id: dict[str, dict] = {}
        self._exact: dict[str, dict[str, str]] = {}
        self._loose: dict[str, dict[str, str]] = {}

        for group in stats_payload["result"]:
            label = group.get("label", "")
            exact = self._exact.setdefault(label, {})
            loose = self._loose.setdefault(label, {})
            for entry in group["entries"]:
                sid = entry["id"]
                self.by_id[sid] = {"id": sid, "text": entry["text"], "group": label}
                tpl = normalise(entry["text"])
                # First writer wins: entries are ordered by trade-site relevance.
                exact.setdefault(tpl, sid)
                loose.setdefault(_signless(tpl), sid)

    def resolve(self, text: str, domain: str = "explicit") -> "tuple[str | None, float]":
        """Resolve a rolled mod line to ``(stat_id, value_sign)``.

        ``value_sign`` is -1 when the item's wording is the inverse of the
        trade site's canonical form. The game writes ``32% reduced Attribute
        Requirements``; trade stores only ``#% increased Attribute
        Requirements``, so the roll has to be negated to mean the same thing.
        Getting this wrong silently inverts every threshold comparison.
        """
        groups = self.DOMAIN_GROUPS.get(domain, ("Explicit",))
        for candidate, sign in _candidates(text):
            for group in groups:
                sid = self._exact.get(group, {}).get(candidate)
                if sid:
                    return sid, sign
            loose = _signless(candidate)
            for group in groups:
                sid = self._loose.get(group, {}).get(loose)
                if sid:
                    return sid, sign
        return None, 1.0

    def lookup(self, text: str, domain: str = "explicit") -> str | None:
        """Resolve a rolled mod line to a stat id, or None if unrecognised."""
        return self.resolve(text, domain)[0]

    def text_for(self, stat_id: str) -> str:
        entry = self.by_id.get(stat_id)
        return entry["text"] if entry else stat_id


def values_in(text: str) -> list[float]:
    """Every number in a mod line, in order."""
    return [float(m.group(0)) for m in _NUM.finditer(text.replace("\n", " "))]


def load_stat_index(force: bool = False) -> StatIndex:
    return StatIndex(fetch("stats", force=force))


def base_type_names(force: bool = False) -> list[str]:
    """Every craftable base type, uniques excluded, in trade-site order.

    Entries carrying a ``name`` are unique items rather than bases - a unique
    is priced by its name, not its rolls, so it has no place in a base survey.
    """
    data = fetch("items", force=force)
    out: list[str] = []
    seen: set[str] = set()
    for group in data.get("result", []):
        for entry in group.get("entries", []):
            t = str(entry.get("type") or "").strip()
            if t and not entry.get("name") and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out


def base_types(force: bool = False) -> set[str]:
    """Every base type name the trade site knows, lowercased.

    Used by ``validate-rules``: a misspelled base in the ruleset matches
    nothing and fails silently, which is the failure mode that command exists
    to catch.
    """
    data = fetch("items", force=force)
    return {
        str(entry["type"]).strip().lower()
        for group in data.get("result", [])
        for entry in group.get("entries", [])
        if entry.get("type")
    }
