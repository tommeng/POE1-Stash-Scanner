"""Render a ScanReport to a standalone HTML file."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import REPORT_DIR, ensure_dirs
from .scanner import ScanReport

TEMPLATE_DIR = Path(__file__).parent / "templates"


def render(report: ScanReport, out_path: Path | str | None = None) -> Path:
    ensure_dirs()
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html")
    html = template.render(
        report=report,
        generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    if out_path is None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = REPORT_DIR / f"scan-{stamp}.html"
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
