"""`validate-rules` is the only thing standing between a typo and a silently
dead rule, so what it does *not* check matters as much as what it does.

Every condition kind fails closed: an unknown flag, pseudo field or condition
key simply never matches, with no error, forever. These tests pin that each of
those is now caught.
"""

import types

import pytest
import yaml

from poescan.cli import cmd_validate
from poescan.triage import DEFAULT_RULES


def _run(tmp_path, rules: list[dict]) -> int:
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "settings": {}, "rules": rules}))
    return cmd_validate(types.SimpleNamespace(rules=str(path)))


def test_the_shipped_ruleset_is_clean(index):
    """Guards against the checks themselves raising false alarms."""
    assert cmd_validate(types.SimpleNamespace(rules=str(DEFAULT_RULES))) == 0


def test_a_good_rule_passes(tmp_path, index):
    assert _run(tmp_path, [{
        "id": "ok", "score": 5,
        "all": [{"pseudo": "life", "min": 90}, {"flag": "influenced"}],
    }]) == 0


@pytest.mark.parametrize(
    "cond,what",
    [
        ({"flag": "influencd"}, "misspelled flag"),
        ({"pseudo": "lfe", "min": 90}, "misspelled pseudo field"),
        ({"mod": "+# to maximum Lifee"}, "mod template that resolves to nothing"),
        ({"stat": "explicit.stat_not_real"}, "unknown stat id"),
        ({"base": "Opel Ring"}, "misspelled base type"),
        ({"category": "accessory.rings"}, "unknown category id"),
        ({"mod_matches": "increased Fizzbuzz Damage"}, "regex matching no real stat"),
        ({"psuedo": "life", "min": 90}, "misspelled condition key"),
        ({"mod_matches": "increased ("}, "invalid regex"),
    ],
)
def test_a_broken_condition_is_caught(tmp_path, index, cond, what):
    assert _run(tmp_path, [{"id": "bad", "score": 5, "all": [cond]}]) == 1, what


def test_unknown_rule_level_category_is_caught(tmp_path, index):
    assert _run(tmp_path, [{
        "id": "bad", "score": 5,
        "categories": ["accessory.rings"],
        "all": [{"pseudo": "life", "min": 90}],
    }]) == 1


def test_mod_matches_is_compared_without_regard_to_sign(tmp_path, index):
    """The game writes '-#% to Fire Resistance'; trade stores the canonical
    '+#%'. A literal comparison reports a false alarm on a correct rule."""
    assert _run(tmp_path, [{
        "id": "helm-enemy-res", "score": 30,
        "all": [{"mod_matches": r"-#% to Fire Resistance"}],
    }]) == 0


def test_mod_matches_rules_are_actually_reached(index):
    """Regression: mod_matches went unchecked entirely, so the eleven
    highest-scoring rules in the ruleset were never validated at all."""
    data = yaml.safe_load(DEFAULT_RULES.read_text())
    using = [
        r for r in data["rules"]
        if any("mod_matches" in c for k in ("all", "any", "none") for c in (r.get(k) or []))
    ]
    assert len(using) > 10, "ruleset should still exercise this path"
    top = sorted(data["rules"], key=lambda r: -r.get("score", 0))[:12]
    assert sum(1 for r in top if r in using) >= 8
