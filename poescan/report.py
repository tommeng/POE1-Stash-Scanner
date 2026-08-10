"""Render a scan, or the ruleset itself, to a standalone HTML file.

Both pages are one self-contained file: inline CSS, no JavaScript, no external
assets. A report you cannot open from a USB stick in six months is not a report.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from .config import REPORT_DIR, ensure_dirs
from .explain import slug

if TYPE_CHECKING:
    # `ScanReport` is needed for an annotation and nothing else, and importing
    # it for real drags the whole scanner graph - trade, stash, cache, httpx -
    # in behind it. That was merely wasteful when this module rendered scans
    # only; now that it also renders a ruleset page which touches none of them,
    # it would be a network dependency on a purely offline command.
    from .explain import Evidence, RulesetDoc
    from .scanner import ScanReport

TEMPLATE_DIR = Path(__file__).parent / "templates"


_CODE_SPAN = re.compile(r"`([^`]+)`")


def codespans(text: str) -> Markup:
    """Backtick spans as ``<code>``, on text that is escaped first.

    The prose this runs on is assembled from the ruleset, so it can contain a
    rule id, a mod template or a regex the user wrote. Escaping happens *before*
    any tag is inserted, so the only markup in the result is the markup added
    here - a `<script>` in a rule cannot reach the page through this path.
    """
    return Markup(_CODE_SPAN.sub(r"<code>\1</code>", str(escape(text))))


def _env() -> Environment:
    """One template loader and one autoescape policy for every page.

    Autoescape is not optional: mod text, item names and rule regexes all come
    from data the user did not write.
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["codespans"] = codespans
    # A rule id is not necessarily its own fragment id: the evidence tables link
    # to cards by id, and computing the anchor twice by two rules is how a
    # summary table full of dead links happens.
    env.filters["ruleanchor"] = lambda rule_id: f"rule-{slug(rule_id)}"
    return env


def _write(html: str, out_path: Path | str) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def render(report: "ScanReport", out_path: Path | str | None = None) -> Path:
    """One scan, as a page. Defaults to a timestamped file in the report dir."""
    html = _env().get_template("report.html").render(report=report, generated=_now())
    if out_path is None:
        ensure_dirs()
        out_path = REPORT_DIR / f"scan-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
    return _write(html, out_path)


def render_rules(
    doc: "RulesetDoc",
    evidence: "Evidence | None" = None,
    league: str = "",
    out_path: Path | str | None = None,
) -> Path:
    """The ruleset, as a page. One file, overwritten each time.

    Deliberately not ``rules-<fingerprint>.html``: that litters one file per
    ruleset *edit*, which is exactly the usage pattern of a command you run
    while tuning. The fingerprint goes in the page, which is where it identifies
    the ruleset anyway.
    """
    html = _env().get_template("rules.html").render(
        doc=doc, evidence=evidence, league=league, generated=_now()
    )
    if out_path is None:
        ensure_dirs()
        out_path = REPORT_DIR / "rules.html"
    return _write(html, out_path)
