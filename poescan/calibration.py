"""Reading accumulated market checks back as evidence about the ruleset.

Pure arithmetic over the observation dicts ``Cache.observations`` returns, kept
apart from the command that renders it so the maths can be tested. This is the
instrument the ruleset's scores are supposed to be corrected by, which makes it
the last code in the repo that should be going unchecked.

**Rules are judged on their tail, not their median.** Triage is a filter, and a
filter is judged by what it wrongly discards. Those are different questions and
they get different answers: measured over 100 priced observations, 12 of the 16
items worth 50c or more scored in the *lowest* band, and the single most
valuable item in the set - an 820 chaos Platinum Kris - scored 10 against a
``promote_score`` of 10, clearing the bar by exactly zero.

A median column cannot see any of that. It reports the typical item a rule
fires on, while the reason to keep the rule is the atypical one. ``fractured``
is the standing example: seven observations at ``1c, 1c, 1c, 1c, 4.5c, 100c,
202.5c``, a median that says delete it and a tail that says the opposite. So
every grouping here reports ``n``, ``median``, ``best`` and ``tail`` together,
and sorts on tail count first.

None of this makes a thin sample thick. ``n`` is reported beside every number
for that reason, and a row backed by two items is an anecdote whichever column
you read it in.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass

# Chaos value at which an item stops being ordinary. Items above this are what
# triage exists to not miss; the exact figure is a reporting choice, not a
# measured constant, and `analyse --tail` moves it.
TAIL_THRESHOLD = 50.0

# Triage score bands, as (low, high) with an open top. These mirror no rule -
# they exist to answer "does a higher score mean a better item", which the
# ruleset claims it does not.
SCORE_BANDS: tuple[tuple[int, int | None], ...] = (
    (0, 10),
    (10, 15),
    (15, 20),
    (20, 30),
    (30, None),
)

# What `analyse` calls an observation whose ruleset fingerprint was never
# recorded, i.e. one written before fingerprinting existed.
UNLABELLED = "unlabelled"


@dataclass(frozen=True)
class Group:
    """One row of a calibration table: a key, and how its items sold."""

    key: str
    n: int
    median: float | None
    best: float | None
    tail: int
    threshold: float = TAIL_THRESHOLD

    @property
    def tail_share(self) -> float:
        return self.tail / self.n if self.n else 0.0


def median(values) -> float | None:
    vals = sorted(v for v in values if v is not None)
    return statistics.median(vals) if vals else None


def priced(rows: list[dict]) -> list[dict]:
    """Observations that produced a usable price.

    A check that matched nothing is a real result about scarcity but not a
    price, and averaging it in as a zero would be a lie about the market.
    """
    return [r for r in rows if r.get("median") is not None]


def baseline(rows: list[dict]) -> float | None:
    """The median priced observation - what an ordinary promoted item is worth."""
    return median([r["median"] for r in rows])


def summarise(key: str, prices: list[float], threshold: float = TAIL_THRESHOLD) -> Group:
    usable = [p for p in prices if p is not None]
    return Group(
        key=key,
        n=len(usable),
        median=median(usable),
        best=max(usable) if usable else None,
        tail=sum(1 for p in usable if p >= threshold),
        threshold=threshold,
    )


def _ranked(groups: list[Group]) -> list[Group]:
    """Best tail first, then best median - the order the filter is judged in."""
    return sorted(groups, key=lambda g: (-g.tail, -(g.median or 0.0), g.key))


def by_rule(
    rows: list[dict], threshold: float = TAIL_THRESHOLD, min_samples: int = 1
) -> list[Group]:
    """Each rule, against the prices of the items it fired on.

    An item hitting three rules counts once for each: rules are additive and
    overlapping, so this measures a rule's company, never its contribution in
    isolation. Two rules firing on identical populations is itself worth
    seeing - it is how nested bands were caught pretending to be independent.
    """
    buckets: dict[str, list[float]] = {}
    for r in rows:
        for rid in r.get("rules_hit") or []:
            buckets.setdefault(str(rid), []).append(r["median"])
    return _ranked(
        [summarise(k, v, threshold) for k, v in buckets.items() if len(v) >= min_samples]
    )


def by_base(
    rows: list[dict], threshold: float = TAIL_THRESHOLD, min_samples: int = 1
) -> list[Group]:
    """Each base type seen, against what those items sold for.

    Note this is what *your stash* holds, not what a base is worth: a base you
    never pick up cannot appear, however valuable. `survey-bases` and
    `base-values` answer the other question.
    """
    buckets: dict[str, list[float]] = {}
    for r in rows:
        base = r.get("base_type")
        if base:
            buckets.setdefault(str(base), []).append(r["median"])
    return _ranked(
        [summarise(k, v, threshold) for k, v in buckets.items() if len(v) >= min_samples]
    )


def by_score_band(
    rows: list[dict],
    threshold: float = TAIL_THRESHOLD,
    bands: tuple[tuple[int, int | None], ...] = SCORE_BANDS,
) -> list[Group]:
    """Triage score against price, in bands, in band order.

    Deliberately *not* ranked by tail: the question here is whether the column
    rises with the score, which only reads if the rows stay in score order.
    """
    out: list[Group] = []
    for lo, hi in bands:
        got = [
            r["median"]
            for r in rows
            if lo <= (r.get("triage_score") or 0) < (hi if hi is not None else float("inf"))
        ]
        if not got:
            continue
        out.append(summarise(f"{lo}-{hi - 1}" if hi else f"{lo}+", got, threshold))
    return out


def tail_below(
    rows: list[dict], score: float, threshold: float = TAIL_THRESHOLD
) -> tuple[int, int]:
    """(valuable items scoring under ``score``, valuable items in total).

    The direct measure of what raising ``promote_score`` would cost. A large
    first number means the score is not separating the items worth keeping from
    the rest, and the cheap-looking fix of demanding a higher score to earn a
    market check would discard exactly them.
    """
    valuable = [r for r in rows if (r.get("median") or 0) >= threshold]
    return sum(1 for r in valuable if (r.get("triage_score") or 0) < score), len(valuable)


def never_fired(rows: list[dict], ruleset) -> list[str]:
    """Rules in the ruleset that no priced observation has ever hit.

    Not evidence against them - an unfired rule has not been measured, which
    is a different thing from having been measured and found worthless.
    """
    seen = {str(rid) for r in rows for rid in (r.get("rules_hit") or [])}
    return [str(r.get("id", "?")) for r in ruleset.rules if str(r.get("id", "?")) not in seen]


# -- ruleset vocabulary -----------------------------------------------------


def vocabularies(rows: list[dict]) -> list[tuple[str, int]]:
    """Which rulesets produced these observations, commonest first.

    More than one entry means the rule tables would be pooling incompatible
    labels if they were read together.
    """
    counts = Counter(str(r.get("ruleset") or UNLABELLED) for r in rows)
    return counts.most_common()


def matching(rows: list[dict], fingerprint: str) -> list[dict]:
    """Only the observations labelled by the ruleset in hand."""
    return [r for r in rows if str(r.get("ruleset") or UNLABELLED) == fingerprint]


def stale(rows: list[dict], fingerprint: str) -> list[dict]:
    """Observations labelled by some other ruleset, or by none.

    These keep their price - it was really observed - but their ``rules_hit``
    refers to a ruleset that is not the one being analysed. One ordinary
    ``poescan scan`` over the same tabs re-labels them for zero API calls,
    because a cache hit already has a freshly scored verdict in hand.
    """
    return [r for r in rows if str(r.get("ruleset") or UNLABELLED) != fingerprint]
