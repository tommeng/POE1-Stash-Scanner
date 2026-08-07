"""Command line interface."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from . import calibration
from .auth import AuthError, authorize, get_token
from .cache import Cache
from .config import (
    DATA_DIR,
    RATELIMIT_STATE,
    Config,
    account_problem,
    clean_poesessid,
    ensure_dirs,
    poesessid_problem,
)
from . import ninja
from .items import CATEGORY_LABELS, classify
from .report import render
from .ratelimit import RateLimiter
from .scanner import open_stash, scan, select_tabs
from .trade import DEFAULT_STATUS, MIN_CONFIDENT_SAMPLE, STATUS_OPTIONS, TradeClient
from .stash import StashError
from .tradedata import (
    base_type_names,
    base_types,
    fetch as fetch_tradedata,
    load_stat_index,
    normalise,
    signless,
)
from .triage import (
    CONDITION_KEYS,
    DEFAULT_RULES,
    FLAG_NAMES,
    PSEUDO_NAMES,
    Ruleset,
    ambiguous_conditions,
    assess,
)

console = Console()


def _err(msg: str) -> int:
    console.print(f"[bold red]error[/] {msg}")
    return 1


NOT_CONFIGURED = (
    "Not configured. Pick one of:\n"
    "  [bold]OAuth[/] (preferred, if your account can register a client at\n"
    "        https://www.pathofexile.com/my-account/applications - type Public,\n"
    "        redirect http://127.0.0.1:8888/callback, scope account:stashes):\n"
    "    poescan setup --client-id <id>\n"
    "    poescan login\n"
    "  [bold]Session cookie[/] (fallback - GGG are not processing new app\n"
    "        registrations; copy POESESSID from your browser cookies):\n"
    "    poescan setup --account 'YourName#1234' --poesessid <cookie>"
)


def _auth(cfg):
    """Token for OAuth installs; None when using the cookie fallback."""
    if cfg.auth_mode == "poesessid":
        return None
    return get_token(cfg)


# ---------------------------------------------------------------------------


def cmd_setup(args) -> int:
    cfg = Config.load()
    if args.client_id:
        cfg.client_id = args.client_id.strip()
    if args.league:
        cfg.league = args.league.strip()
    if args.port:
        cfg.redirect_port = int(args.port)
    if args.tabs is not None:
        cfg.tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    if args.poesessid:
        cfg.poesessid = clean_poesessid(args.poesessid)
    if args.account:
        cfg.account_name = args.account.strip()
    cfg.save()

    problems = []
    if cfg.poesessid and (why := poesessid_problem(cfg.poesessid)):
        problems.append(f"POESESSID looks wrong: {why}")
    if cfg.account_name and (why := account_problem(cfg.account_name)):
        problems.append(f"Account name looks wrong: {why}")

    console.print("[green]Saved.[/]")
    console.print(f"  client_id     {cfg.client_id or '[dim]not set[/]'}")
    console.print(f"  league        {cfg.league}")
    console.print(f"  redirect_uri  {cfg.redirect_uri}")
    console.print(f"  tabs          {', '.join(cfg.tabs) if cfg.tabs else '[dim]all[/]'}")
    console.print(f"  account       {cfg.account_name or '[dim]not set[/]'}")
    # Never print the cookie itself.
    console.print(f"  poesessid     {'[green]set[/]' if cfg.poesessid else '[dim]not set[/]'}")
    console.print(f"  auth mode     [bold]{cfg.auth_mode}[/]")

    for msg in problems:
        console.print(f"\n[bold red]![/] {msg}")
    if problems:
        console.print(
            "\n[dim]In your browser on pathofexile.com: devtools -> Application ->\n"
            "Cookies -> https://www.pathofexile.com -> the row named exactly\n"
            "[/][bold]POESESSID[/][dim]. Copy its Value column (32 hex chars).[/]"
        )
    elif cfg.auth_mode == "poesessid":
        console.print("\n[green]Ready.[/] Using POESESSID auth. Next: "
                      "[bold]poescan diagnose[/]")
    elif not cfg.client_id:
        console.print(
            "\n[yellow]Next:[/] register a client at "
            "[link]https://www.pathofexile.com/my-account/applications[/]\n"
            "  Application type : [bold]Public[/]\n"
            f"  Redirect URI     : [bold]{cfg.redirect_uri}[/]\n"
            "  Scopes           : [bold]account:stashes[/]\n"
            "then run: [bold]poescan setup --client-id <id>[/]\n\n"
            "[dim]GGG are currently not processing new application registrations. "
            "If you cannot register one, use the session-cookie fallback:[/]\n"
            "  [bold]poescan setup --account 'Name#1234' --poesessid <cookie>[/]"
        )
    return 0


def cmd_login(args) -> int:
    cfg = Config.load()
    if not cfg.client_id:
        return _err("No client_id set. Run `poescan setup --client-id <id>` first.")
    console.print("Opening your browser to authorise poescan...")
    try:
        token = authorize(cfg, open_browser=not args.no_browser)
    except AuthError as e:
        return _err(str(e))
    hours = (token.expires_at - time.time()) / 3600
    console.print(f"[green]Logged in[/] as [bold]{token.username or 'your account'}[/].")
    console.print(f"Token valid for ~{hours:.0f}h; refresh happens automatically for 7 days.")
    return 0


def cmd_tabs(args) -> int:
    cfg = Config.load()
    if cfg.auth_mode == "none":
        return _err(NOT_CONFIGURED)
    try:
        token = _auth(cfg)
    except AuthError as e:
        return _err(str(e))

    # Printed nothing at all while it waited, which is the worst version of the
    # problem: this is the command people run straight after a scan, i.e. exactly
    # when the stash window is spent and the wait is longest.
    limiter = RateLimiter(state_path=RATELIMIT_STATE)
    try:
        with console.status("[bold]Listing tabs...") as status, open_stash(cfg, token, limiter) as stash:
            limiter.on_wait = lambda w: status.update(f"[bold]Listing tabs - {w.describe()}")
            tabs = stash.flat_tabs()
    except StashError as e:
        return _err(str(e))

    from .scanner import is_gear_tab

    dupes = {n for n in (t.name.lower() for t in tabs)
             if sum(1 for t in tabs if t.name.lower() == n) > 1}

    table = Table(title=f"Stash tabs - {cfg.league}", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name")
    table.add_column("Type", style="dim")
    table.add_column("Scanned", justify="center")
    for t in tabs:
        gear = is_gear_tab(t)
        name = t.name + ("  [yellow](duplicate name)[/]" if t.name.lower() in dupes else "")
        table.add_row(
            str(t.index),
            name if gear else f"[dim]{name}[/]",
            t.type,
            "[green]yes[/]" if gear else "[dim]no[/]",
        )
    console.print(table)
    console.print(
        "\nBy default every [green]yes[/] tab is scanned. To pick specific ones:\n"
        "  [bold]poescan scan --tabs \"Misc,To sell\"[/]   exact names\n"
        "  [bold]poescan scan --tabs \"#20,#33\"[/]         by index - the only way to\n"
        "                                          separate duplicate names"
    )
    return 0


def cmd_scan(args) -> int:
    cfg = Config.load()
    if args.league:
        cfg.league = args.league
    if cfg.auth_mode == "none":
        return _err(NOT_CONFIGURED)

    try:
        token = _auth(cfg)
    except AuthError as e:
        return _err(str(e))

    try:
        ruleset = Ruleset.load(args.rules)
    except (OSError, ValueError) as e:
        return _err(f"Could not load ruleset: {e}")

    if args.promote_score is not None:
        ruleset.promote_score = float(args.promote_score)

    console.print("Loading trade stat definitions...")
    try:
        index = load_stat_index()
    except Exception as e:  # noqa: BLE001 - httpx, or a malformed payload
        return _err(f"Could not load trade stat definitions: {e}")

    tabs_wanted = [t.strip() for t in args.tabs.split(",")] if args.tabs else None

    with console.status("[bold]Scanning...") as status:
        try:
            report = scan(
                cfg,
                token,
                index,
                ruleset,
                tabs_wanted=tabs_wanted,
                verify=not args.no_verify,
                max_checks=args.max_checks,
                cache_hours=0 if args.fresh else args.cache_hours,
                status=args.status,
                progress=lambda m: status.update(f"[bold]{m}"),
            )
        except StashError as e:
            return _err(str(e))

    _print_summary(report)

    if args.json:
        import json

        payload = {
            "league": report.league,
            "tabs": report.tabs_scanned,
            "total_items": report.total_items,
            "rare_items": report.rare_items,
            "candidates": [
                {
                    # The handle for `poescan dismiss`. Without it there was no
                    # way to name an item from outside the scan at all.
                    "id": c.item.id,
                    "name": c.item.label,
                    "category": c.item.category_label,
                    "ilvl": c.item.ilvl,
                    "tab": c.item.tab_name,
                    "position": c.item.position,
                    "interest": round(c.interest, 1),
                    "triage_score": c.verdict.score,
                    "reasons": c.verdict.reasons,
                    "mods": [m.text for m in c.item.mods],
                    "market": None
                    if not c.market
                    else {
                        "total": c.market.total,
                        "cheapest_chaos": c.market.cheapest,
                        "median_chaos": c.market.median,
                        "url": c.market.query_url,
                    },
                }
                for c in report.candidates
            ],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2))
        console.print(f"JSON written to [bold]{args.json}[/]")

    if not args.no_html:
        path = render(report, args.out)
        console.print(f"\nReport: [bold]{path}[/]")
        if not args.no_open:
            webbrowser.open(path.as_uri())
    return 0


def _print_summary(report) -> None:
    console.print()
    if report.scanned_label:
        console.print(f"[dim]Scanned:[/] {report.scanned_label}")
    console.print(
        f"[dim]{report.total_items} items[/] "
        f"[dim]/[/] [bold]{report.rare_items}[/] rare "
        f"[dim]/[/] [bold yellow]{len(report.candidates)}[/] flagged "
        f"[dim]({report.market_checks} market checks, {report.cache_hits} cached, "
        + (f"{report.dismissed_items} dismissed, " if report.dismissed_items else "")
        + f"{report.duration:.0f}s)[/]"
    )
    for n in report.notes:
        console.print(f"[yellow]![/] {n}")

    if not report.candidates:
        console.print("\n[dim]Nothing crossed the threshold.[/]")
        return

    table = Table(header_style="bold", show_lines=False)
    table.add_column("", justify="right", style="dim", width=3)
    table.add_column("Item", max_width=38)
    table.add_column("Where", style="dim", max_width=22)
    table.add_column("Market", max_width=30)
    table.add_column("Why", max_width=40)

    for i, c in enumerate(report.candidates[:25], 1):
        m = c.market
        if not m:
            market = "[dim]not checked[/]"
        elif m.error:
            market = f"[yellow]{m.error}[/]"
        elif m.total == 0:
            market = "[yellow]nothing comparable[/]"
        else:
            med = m.median
            colour = "green" if med and med >= 30 else "yellow" if med and med >= 8 else "red"
            market = f"[{colour}]{m.total} listed" + (f", med {med:.0f}c" if med else "") + "[/]"
        table.add_row(
            str(i),
            f"[yellow]{c.item.label}[/]\n[dim]{c.item.category_label} ilvl {c.item.ilvl}[/]",
            f"{c.item.tab_name}\n{c.item.position}",
            market,
            "; ".join(c.verdict.reasons[:2]),
        )
    console.print()
    console.print(table)


def cmd_diagnose(args) -> int:
    """Pre-flight check: what does triage actually see in your tabs?

    Reads the stash but makes zero trade API calls, so it is free and instant.
    Surfaces the three ways this tool fails silently: items whose category was
    not recognised (their rules never fire), mods that resolved to no trade
    stat (invisible to triage and unsearchable), and a score distribution that
    shows whether the promotion threshold is anywhere near right.
    """
    cfg = Config.load()
    if args.league:
        cfg.league = args.league
    if cfg.auth_mode == "none":
        return _err(NOT_CONFIGURED)
    try:
        token = _auth(cfg)
    except AuthError as e:
        return _err(str(e))

    try:
        ruleset = Ruleset.load(args.rules)
    except (OSError, ValueError) as e:
        return _err(f"Could not load ruleset: {e}")
    try:
        index = load_stat_index()
    except Exception as e:  # noqa: BLE001 - httpx, or a malformed payload
        return _err(f"Could not load trade stat definitions: {e}")
    tabs_wanted = [t.strip() for t in args.tabs.split(",")] if args.tabs else list(cfg.tabs or [])

    limiter = RateLimiter(state_path=RATELIMIT_STATE)
    try:
        with console.status("[bold]Reading stash...") as status, open_stash(cfg, token, limiter) as stash:
            # Reading several tabs can park on the stash window for minutes.
            limiter.on_wait = lambda w: status.update(f"[bold]Reading stash - {w.describe()}")
            chosen = select_tabs(stash.flat_tabs(), tabs_wanted)
            if not chosen:
                return _err(f"No tabs matched {tabs_wanted!r}. Run `poescan tabs` to list them.")
            items = [i for tab in chosen for i in stash.tab_items(tab, index)]
    except StashError as e:
        return _err(str(e))

    rares = [i for i in items if i.rarity == "rare"]
    console.print(f"\n[bold]{len(items)}[/] items, [bold]{len(rares)}[/] rare, "
                  f"across {len(chosen)} tab(s): {', '.join(t.name for t in chosen)}\n")

    # 1. Category coverage
    unknown = Counter(i.base_type for i in rares if i.category == "unknown")
    if unknown:
        t = Table(title="[red]Unrecognised item categories[/] (their rules never fire)",
                  header_style="bold")
        t.add_column("base type")
        t.add_column("count", justify="right")
        for base, n in unknown.most_common(20):
            t.add_row(base, str(n))
        console.print(t)
        console.print(f"[red]{sum(unknown.values())} of {len(rares)} rares "
                      f"({100 * sum(unknown.values()) / max(1, len(rares)):.0f}%) unclassified[/]\n")
    else:
        console.print("[green]All rares classified into a known category.[/]\n")

    # 2. Mod resolution coverage
    unresolved = Counter(m.text for i in rares for m in i.mods if not m.stat_id)
    if unresolved:
        t = Table(title="[yellow]Mods that mapped to no trade stat[/] (invisible to triage)",
                  header_style="bold")
        t.add_column("mod text", max_width=64)
        t.add_column("count", justify="right")
        for text, n in unresolved.most_common(25):
            t.add_row(text, str(n))
        console.print(t)
        total_mods = sum(len(i.mods) for i in rares)
        console.print(f"[yellow]{sum(unresolved.values())} of {total_mods} mod lines "
                      f"({100 * sum(unresolved.values()) / max(1, total_mods):.1f}%) unresolved[/]\n")
    else:
        console.print("[green]Every mod resolved to a trade stat.[/]\n")

    # 3. Score distribution vs the threshold
    verdicts = [assess(i, ruleset) for i in rares]
    scored = [v for v in verdicts if not v.rejected]
    bands = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 60), (60, 10**6)]
    t = Table(title="Triage score distribution", header_style="bold")
    t.add_column("score band")
    t.add_column("items", justify="right")
    t.add_column("", max_width=34)
    peak = max((sum(1 for v in scored if lo <= v.score < hi) for lo, hi in bands), default=1)
    for lo, hi in bands:
        n = sum(1 for v in scored if lo <= v.score < hi)
        label = f"{lo}-{hi}" if hi < 10**6 else f"{lo}+"
        mark = " <- threshold" if lo == int(ruleset.promote_score) else ""
        t.add_row(label + mark, str(n), "#" * int(30 * n / max(1, peak)))
    console.print(t)

    rejected = len(verdicts) - len(scored)
    promoted = sum(1 for v in verdicts if v.promoted)
    console.print(
        f"\n[dim]{rejected} vetoed outright, {len(scored)} scored, "
        f"[/][bold yellow]{promoted}[/] would be promoted at "
        f"promote_score={ruleset.promote_score:.0f}"
    )
    if promoted > 60:
        console.print(f"[yellow]![/] That is a lot to sift. Try "
                      f"[bold]--promote-score {ruleset.promote_score + 8:.0f}[/]")
    elif promoted == 0:
        console.print(f"[yellow]![/] Nothing promoted. Try "
                      f"[bold]--promote-score {max(10, ruleset.promote_score - 8):.0f}[/]")

    # Which rules are actually doing the work
    fired = Counter(h.rule_id for v in scored for h in v.hits)
    if fired:
        t = Table(title="Rules that fired", header_style="bold")
        t.add_column("rule")
        t.add_column("times", justify="right")
        for rid, n in fired.most_common(15):
            t.add_row(rid, str(n))
        console.print(t)
    unused = [r.get("id") for r in ruleset.rules if r.get("id") not in fired]
    if unused:
        console.print(f"[dim]Never fired: {', '.join(str(u) for u in unused[:12])}"
                      f"{' ...' if len(unused) > 12 else ''}[/]")
    return 0


def cmd_validate(args) -> int:
    """Check every mod template and base type in the ruleset really exists."""
    import re as _re

    import yaml

    path = Path(args.rules) if args.rules else DEFAULT_RULES
    data = yaml.safe_load(path.read_text()) or {}
    try:
        index = load_stat_index()
    except Exception as e:  # noqa: BLE001 - httpx, or a malformed payload
        return _err(f"Could not load trade stat definitions: {e}")
    known_bases = base_types()
    stat_texts = [signless(e["text"]) for e in index.by_id.values()]

    problems: list[tuple[str, str, str]] = []
    checked = 0
    for section in ("veto", "rules"):
        for rule in data.get(section) or []:
            rid = str(rule.get("id", "?"))
            for key in ("all", "any", "none"):
                for cond in rule.get(key) or []:
                    if not isinstance(cond, dict):
                        continue
                    templates = []
                    for k in ("mod", "mod_any"):
                        v = cond.get(k)
                        if isinstance(v, str):
                            templates.append(v)
                        elif isinstance(v, list):
                            templates.extend(v)
                    for t in templates:
                        checked += 1
                        if normalise(t) != t:
                            problems.append((rid, t, f"not in template form (should be '{normalise(t)}')"))
                        elif not any(index.lookup(t, d) for d in ("explicit", "implicit", "crafted")):
                            problems.append((rid, t, "no matching trade stat"))
                    for sid in _as_list(cond.get("stat")) + _as_list(cond.get("stat_any")):
                        checked += 1
                        if sid not in index.by_id:
                            problems.append((rid, sid, "unknown stat id"))
                    # A misspelled base matches nothing and fails silently.
                    for b in _as_list(cond.get("base")) + _as_list(cond.get("base_any")):
                        checked += 1
                        if str(b).strip().lower() not in known_bases:
                            problems.append((rid, b, "unknown base type"))
                    if "base_matches" in cond:
                        checked += 1
                        pattern = str(cond["base_matches"])
                        try:
                            rx = _re.compile(pattern, _re.IGNORECASE)
                        except _re.error as e:
                            problems.append((rid, pattern, f"invalid regex: {e}"))
                        else:
                            if not any(rx.search(b) for b in known_bases):
                                problems.append((rid, pattern, "matches no known base type"))
                    # mod_matches carries the highest-scoring rules in the
                    # ruleset and went unchecked entirely until now. Compared
                    # sign-agnostically: the game writes "-#% to Fire
                    # Resistance" where trade stores the canonical "+#%", so a
                    # literal comparison reports false alarms.
                    if "mod_matches" in cond:
                        checked += 1
                        pattern = str(cond["mod_matches"])
                        try:
                            rx = _re.compile(signless(pattern), _re.IGNORECASE)
                        except _re.error as e:
                            problems.append((rid, pattern, f"invalid regex: {e}"))
                        else:
                            if not any(rx.search(t) for t in stat_texts):
                                problems.append((rid, pattern, "matches no known trade stat"))
                    # These three all fail closed and silently: a typo means the
                    # rule simply never fires, with no error, forever.
                    if "flag" in cond:
                        checked += 1
                        if str(cond["flag"]) not in FLAG_NAMES:
                            problems.append((rid, str(cond["flag"]), "unknown flag name"))
                    if "pseudo" in cond:
                        checked += 1
                        if str(cond["pseudo"]) not in PSEUDO_NAMES:
                            problems.append((rid, str(cond["pseudo"]), "unknown pseudo field"))
                    for cat in _as_list(cond.get("category")):
                        checked += 1
                        if cat not in CATEGORY_LABELS:
                            problems.append((rid, cat, "unknown category id"))

                    unknown = set(cond) - CONDITION_KEYS
                    for key in sorted(unknown):
                        checked += 1
                        problems.append((rid, key, "unknown condition key - matches nothing"))

            for cat in _as_list(rule.get("categories")) + _as_list(rule.get("exclude_categories")):
                checked += 1
                if cat not in CATEGORY_LABELS:
                    problems.append((rid, cat, "unknown category id"))

    # `Ruleset.load` refuses these outright, so reporting them here is what
    # turns a traceback from `scan` into a listed finding - and lists all of
    # them at once rather than just the first.
    for ambiguous in ambiguous_conditions(data):
        checked += 1
        problems.append((ambiguous.rule_id, ambiguous.where, ambiguous.reason))

    if problems:
        console.print(f"[red]{len(problems)} problem(s)[/] in {checked} references:\n")
        for rid, ref, why in problems:
            console.print(f"  [bold]{rid}[/]: [yellow]{ref}[/]\n    {why}")
        return 1
    console.print(f"[green]All {checked} references resolve.[/]")
    return 0


def _as_list(v):
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def cmd_categories(args) -> int:
    table = Table(title="Category ids for use in rules", header_style="bold")
    table.add_column("id")
    table.add_column("label", style="dim")
    for cid, label in sorted(CATEGORY_LABELS.items()):
        table.add_row(cid, label)
    console.print(table)
    return 0


def cmd_budget(args) -> int:
    """Show how much API allowance is left, per policy.

    Uses only persisted state - makes no requests of its own.
    """
    lim = RateLimiter(state_path=RATELIMIT_STATE)
    policies = sorted(lim._buckets)
    if not policies:
        console.print("[dim]No usage recorded yet. Limits are learned from the first response.[/]")
        return 0

    table = Table(title="API allowance remaining", header_style="bold")
    table.add_column("policy")
    table.add_column("window", justify="right")
    table.add_column("used", justify="right")
    table.add_column("left", justify="right")
    table.add_column("")

    for name in policies:
        b = lim.bucket(name)
        now = b.clock()
        blocked = max(0.0, b.blocked_until - now)
        for rule in b.rules:
            used = len([t for t in b.timestamps if t > now - rule.period])
            budget = max(1, rule.hits - b.headroom)
            left = max(0, budget - used)
            bar = "#" * int(20 * used / max(1, budget))
            window = f"{rule.period:g}s" if rule.period < 3600 else f"{rule.period / 3600:g}h"
            colour = "red" if left == 0 else "yellow" if left < budget * 0.25 else "green"
            table.add_row(
                name.replace("-request-limit", ""),
                window,
                str(used),
                f"[{colour}]{left}[/]",
                f"[dim]{bar}[/]",
            )
        if blocked > 0:
            table.add_row("", "", "", "[red]BANNED[/]", f"[red]{blocked:.0f}s remaining[/]")

    console.print(table)
    console.print(
        "\n[dim]Counts are shared with anything else using your IP/account, and are learned\n"
        "from GGG's own headers. `left` already reserves one request of headroom.[/]"
    )
    return 0


def cmd_cache(args) -> int:
    with Cache() as cache:
        if args.clear:
            # Scoped the same way the summary is, so `--league X --clear`
            # cannot quietly empty every other league's observations as well.
            # Dismissals survive either way: they are the user's judgement
            # rather than cached data, and re-earning one costs an API call.
            if args.league:
                cache.conn.execute("DELETE FROM market_check WHERE league = ?", (args.league,))
            else:
                cache.conn.execute("DELETE FROM market_check")
            cache.conn.commit()
            console.print("[green]Market check cache cleared.[/]")
        s = cache.stats(args.league)
        console.print(f"[dim]scope:[/] {args.league or 'all leagues'}")
        console.print(f"cached market checks: [bold]{s['market_checks']}[/]")
        console.print(f"dismissed items:      [bold]{s['dismissed']}[/]")
    return 0


def _print_dismissals(rows, league: str) -> None:
    if not rows:
        console.print(
            f"[dim]Nothing dismissed in[/] [bold]{league}[/][dim].[/]\n"
            "Every card in the HTML report prints the command to dismiss that item, and\n"
            "[bold]poescan scan --json[/] writes an [bold]id[/] for each candidate."
        )
        return

    table = Table(title=f"Dismissed items - {league}", header_style="bold")
    table.add_column("item id")
    table.add_column("dismissed", style="dim")
    table.add_column("league", style="dim")
    table.add_column("note")
    for d in rows:
        table.add_row(
            d.item_id,
            time.strftime("%Y-%m-%d", time.localtime(d.dismissed_at)),
            "[yellow]every league[/]" if d.every_league else d.league,
            d.note or "[dim]-[/]",
        )
    console.print(table)
    console.print(
        "\n[dim]These never expire. A dismissal is your judgement about the item, not a\n"
        "price observation, so re-showing it in three days is the thing it exists to\n"
        "stop. It is scoped to the league instead, because item ids follow items into\n"
        "Standard when a league ends. Reverse one with[/] "
        "[bold]poescan dismiss --undo <item-id>[/][dim].[/]"
    )


def cmd_dismiss(args) -> int:
    """Stop re-pricing items already judged not worth it.

    Roughly a quarter of the checks in a mature cache re-confirm junk - a
    couple of chaos against hundreds of listings - every time the 72h cache
    expires, and that budget is why a full scan takes half an hour. Dismissing
    an item keeps it out of the report and off the budget while leaving the
    price observation it produced in place, so `analyse` still reads it as the
    negative half of its dataset.

    Manual only, on purpose. Auto-dismissing on the junk signal would buy
    budget with false negatives, which is the failure mode that costs money.
    """
    cfg = Config.load()
    league = args.league or cfg.league
    # Deduplicated so a repeated id cannot report itself both restored and
    # never dismissed, which is what the second pass through it would say.
    ids = list(dict.fromkeys(i.strip() for i in (args.item_ids or []) if i.strip()))

    with Cache() as cache:
        if args.list:
            _print_dismissals(cache.dismissals(league), league)
            return 0

        if not ids:
            return _err(
                "no item ids given.\n"
                "  [bold]poescan dismiss <item-id> --note 'flooded at 1c'[/]\n"
                "  [bold]poescan dismiss --list[/]\n"
                "  [bold]poescan dismiss --undo <item-id>[/]\n"
                "Ids are printed on every card of the HTML report."
            )

        if args.undo:
            restored = [i for i in ids if cache.undismiss(i, league)]
            for i in ids:
                if i in restored:
                    console.print(f"[green]restored[/] {i}")
                else:
                    console.print(f"[dim]was not dismissed in {league}:[/] {i}")
            # Nothing restored means every id was wrong, which is a failure
            # worth an exit code rather than a cheerful no-op.
            return 0 if restored else 1

        for i in ids:
            cache.dismiss(i, league, args.note or "")
            console.print(f"[green]dismissed[/] {i}")
            # A mistyped id dismisses nothing, for ever, and says nothing about
            # it. This is the only moment the mistake is still cheap to notice.
            if not cache.has_check(i, league):
                console.print(
                    f"  [yellow]![/] no market check on record for that id in {league} - "
                    "expected if it was never priced, otherwise check the id"
                )
        console.print(
            f"\n[dim]Future scans in[/] [bold]{league}[/][dim] will skip "
            f"{len(ids)} item(s) and spend no market checks on them.\n"
            "Reverse with[/] [bold]poescan dismiss --undo <item-id>[/][dim].[/]"
        )
    return 0


def cmd_refresh_data(args) -> int:
    for name in ("stats", "items", "filters", "static"):
        fetch_tradedata(name, force=True)
        console.print(f"  refreshed [bold]{name}[/]")
    console.print("[green]Trade metadata updated.[/]")
    return 0


# ---------------------------------------------------------------------------


# The trade API stops counting here; a total at the cap is a floor, not a total.
_API_RESULT_CAP = 10000

# poe.ninja's `count` saturates here. Anything at the cap is a lower bound.
NINJA_SAMPLE_CAP = 399


def _median(values) -> float | None:
    vals = sorted(v for v in values if v is not None)
    return statistics.median(vals) if vals else None


def _price_cell(med: float | None, baseline: float | None) -> str:
    """A median, coloured by how it compares with the overall base rate."""
    if med is None:
        return "[dim]-[/]"
    if baseline and med >= baseline * 2:
        return f"[green]{med:.0f}c[/]"
    if baseline and med <= baseline:
        return f"[red]{med:.0f}c[/]"
    return f"{med:.0f}c"


def _group_rows(table: Table, groups, baseline: float | None, limit: int | None = None) -> None:
    """Render Groups into a table whose columns are already declared."""
    for g in groups[:limit] if limit else groups:
        table.add_row(
            g.key,
            str(g.n),
            _price_cell(g.median, baseline),
            f"{g.best:.0f}c" if g.best is not None else "[dim]-[/]",
            f"[green]{g.tail}[/]" if g.tail else "[dim]0[/]",
        )


def cmd_analyse(args) -> int:
    """Read the accumulated market checks back and ask what they say.

    Every scan already pays for real price observations. This pairs them with
    the items that produced them, so the hand-authored scores can be checked
    against outcomes instead of argued about. Makes no API calls.

    The maths lives in `calibration.py`; this function only renders it.
    """
    cfg = Config.load()
    league = args.league or cfg.league
    threshold = float(args.tail)
    with Cache() as cache:
        rows = cache.observations(league)

    try:
        ruleset = Ruleset.load(args.rules)
    except (OSError, ValueError) as e:
        return _err(f"Could not load ruleset: {e}")

    if not rows:
        console.print(
            f"[yellow]No labelled observations for {league}.[/]\n\n"
            "Checks only carry item features if they were written after feature\n"
            "logging existed. Run a normal [bold]poescan scan[/] over the same tabs:\n"
            "cached checks get re-labelled without spending any API calls."
        )
        return 0

    # -- whose ruleset are these observations even about? -------------------
    #
    # A rule id means nothing without the ruleset that defined it. Pooling two
    # vocabularies produces a table that looks like evidence and is not, which
    # is how the nested life bands went on being reported after being split.
    setaside = calibration.stale(rows, ruleset.fingerprint)
    if setaside:
        vocab = calibration.vocabularies(setaside)
        listed = ", ".join(f"{k} x{n}" for k, n in vocab[:3]) + (", ..." if len(vocab) > 3 else "")
        verb = "Pooling" if args.all_versions else "Setting aside"
        console.print(
            f"\n[yellow]{verb} {len(setaside)} of {len(rows)} observations[/] labelled by a "
            f"ruleset other than the one loaded here ({listed})."
        )
        console.print(
            "[dim]Their prices are real; their rule labels describe rules that may no longer "
            "exist. Run [bold]poescan scan[/] over the same tabs to re-label them for free"
            + (".[/]" if args.all_versions else ", or [bold]--all-versions[/] to pool them.[/]")
        )
        if not args.all_versions:
            rows = calibration.matching(rows, ruleset.fingerprint)

    priced = calibration.priced(rows)
    if not priced:
        console.print(
            f"\n[yellow]No priced observations for ruleset {ruleset.fingerprint}.[/]\n"
            "A scan over the same tabs will re-label the cached ones at no API cost."
        )
        return 0

    baseline = calibration.baseline(priced)
    scope = "pooled" if args.all_versions and setaside else f"ruleset [bold]{ruleset.fingerprint}[/]"
    console.print(
        f"\n[bold]{len(rows)}[/] observations in {league}, "
        f"[bold]{len(priced)}[/] priced, {scope}."
    )
    if baseline is not None:
        lo = min(r["median"] for r in priced)
        hi = max(r["median"] for r in priced)
        console.print(f"Median price [bold]{baseline:.0f}c[/]  (range {lo:.0f}c - {hi:.0f}c)")
    console.print(
        f"[dim]`best` is the top price a row saw; `>={threshold:g}c` counts the items that\n"
        "cleared it. Triage is a filter, so those two columns are the ones that matter -\n"
        "a rule earns its place by what it catches, not by its typical item.[/]\n"
    )
    if len(priced) < 30:
        console.print(
            "[yellow]Small sample.[/] Treat everything below as direction, not calibration -\n"
            "a row backed by two items is an anecdote.\n"
        )

    # -- does the triage score track price at all? --------------------------
    t = Table(title="Triage score vs price", header_style="bold")
    t.add_column("score")
    t.add_column("items", justify="right")
    t.add_column("median", justify="right")
    t.add_column("best", justify="right")
    t.add_column(f">={threshold:g}c", justify="right")
    _group_rows(t, calibration.by_score_band(priced, threshold), baseline)
    console.print(t)

    missed, valuable = calibration.tail_below(priced, ruleset.promote_score + 5, threshold)
    if valuable:
        console.print(
            f"[dim]If this column is flat, the scores are filtering, not ranking - which is\n"
            f"what scanner.py already assumes. A rising column would be new information.[/]\n"
            f"[bold]{missed} of {valuable}[/] items worth {threshold:g}c+ scored under "
            f"{ruleset.promote_score + 5:g}, i.e. in the lowest promoted band.\n"
            f"[dim]That is the cost of raising promote_score, measured rather than argued.[/]\n"
        )

    # -- which rules actually fire on items worth something? ----------------
    t = Table(
        title=f"Rules, by what they caught (>={threshold:g}c first)", header_style="bold"
    )
    t.add_column("rule")
    t.add_column("fired", justify="right")
    t.add_column("median", justify="right")
    t.add_column("best", justify="right")
    t.add_column(f">={threshold:g}c", justify="right")
    _group_rows(t, calibration.by_rule(priced, threshold, args.min_samples), baseline)
    console.print(t)

    never = calibration.never_fired(priced, ruleset)
    if never:
        console.print(
            f"[dim]{len(never)} rules never fired on a priced item - unmeasured, which is not\n"
            "the same as worthless: "
            + ", ".join(never[:12])
            + ("..." if len(never) > 12 else "")
            + "[/]\n"
        )

    # -- base types ---------------------------------------------------------
    t = Table(title="Base types seen", header_style="bold")
    t.add_column("base")
    t.add_column("seen", justify="right")
    t.add_column("median", justify="right")
    t.add_column("best", justify="right")
    t.add_column(f">={threshold:g}c", justify="right")
    _group_rows(t, calibration.by_base(priced, threshold, args.min_samples), baseline, limit=25)
    console.print(t)
    console.print(
        "[dim]Your stash only shows bases you happen to own. For what a base is worth\n"
        "on its own, run [bold]poescan survey-bases[/].[/]"
    )
    return 0


def cmd_base_values(args) -> int:
    """What each base type is worth unrolled, from poe.ninja's aggregate.

    One request covers every slot. `survey-bases` measures the same market
    directly from GGG and agrees on ordering, but its samples are 10-100x
    smaller - see poescan/ninja.py.
    """
    cfg = Config.load()
    # Not resolved here: `fetch_base_types` does it on a cache miss, so a warm
    # cache costs no requests at all rather than one for the league list.
    league = args.league or cfg.league
    try:
        if args.slots:
            for t in ninja.item_types(league):
                console.print(f"  {t}")
            return 0
        if args.influences:
            for i in ninja.influences(league):
                console.print(f"  {i}")
            console.print("  [dim]None[/]  (uninfluenced)")
            return 0
        rows = ninja.base_values(
            league,
            item_type=args.slot,
            influence=args.influence,
            ilvl_min=args.ilvl,
            min_samples=args.min_samples,
            force=args.refresh,
        )
    except ninja.NinjaError as e:
        return _err(str(e))

    if not rows:
        console.print(
            "[yellow]No rows matched.[/] The filters are all valid, so this is a real "
            "empty result.\n"
            f"Note the data only covers item levels 82-86{'' if not args.ilvl else f' - --ilvl {args.ilvl} may be outside that'}."
        )
        return 0

    scope = " / ".join(
        p for p in (args.slot, args.influence, f"ilvl >= {args.ilvl}" if args.ilvl else None) if p
    )
    table = Table(
        title=f"Unrolled base values - {league}{f' ({scope})' if scope else ''}",
        header_style="bold",
    )
    table.add_column("base")
    table.add_column("slot", style="dim")
    table.add_column("influence", style="dim")
    table.add_column("ilvl", justify="right")
    # `samples` is what the price was computed from and caps at 399; `listings`
    # is the market. Showing both stops one being read as the other.
    table.add_column("samples", justify="right")
    table.add_column("listings", justify="right")
    table.add_column("value", justify="right")

    baseline = _median([r.chaos for r in rows])
    for r in rows[: args.top]:
        value = _price_cell(r.chaos, baseline) if r.chaos < 1000 else f"[green]{r.divine:,.1f}div[/]"
        samples = str(r.samples) + ("+" if r.samples >= NINJA_SAMPLE_CAP else "")
        table.add_row(
            r.name,
            r.item_type,
            r.influence or "-",
            str(r.ilvl),
            samples if r.confident else f"[yellow]{samples}[/]",
            f"{r.listings:,}",
            value,
        )
    console.print(table)

    shown = min(args.top, len(rows))
    console.print(
        f"\n[dim]{len(rows)} rows matched, showing {shown}. Rows below "
        f"{args.min_samples} samples are hidden entirely - pass --min-samples 0 to see them,\n"
        "but a single sampled Helical Ring reported 30510c, so they are noise rather than\n"
        f"cheap data. A samples figure of {NINJA_SAMPLE_CAP}+ is poe.ninja's cap, not the\n"
        "market size - read the listings column for that.\n"
        "Same underlying stash data as the trade API, better aggregated - not a second opinion.[/]"
    )
    return 0


def cmd_survey_bases(args) -> int:
    """Measure what each base type in a slot floors at on the open market.

    One search plus one fetch per base, no stat filters, so the only thing
    varying is the base. See TradeClient.base_floor for why the price is read
    rather than the listing count.
    """
    cfg = Config.load()
    league = args.league or cfg.league

    if args.bases:
        bases = [b.strip() for b in args.bases.split(",") if b.strip()]
        known = base_types()
        unknown = [b for b in bases if b.lower() not in known]
        if unknown:
            return _err("unknown base type(s): " + ", ".join(unknown))
    elif args.category:
        bases = [b for b in base_type_names() if classify("", b) == args.category]
        if not bases:
            return _err(
                f"no base types resolve to {args.category!r} by name.\n"
                "Base types are matched by name suffix, which only works for jewellery,\n"
                "wands, sceptres, bows and quivers. For anything else pass --bases with\n"
                "explicit names, e.g. --bases 'Vaal Regalia,Astral Plate'."
            )
    else:
        return _err("pass --category or --bases")

    if args.limit:
        bases = bases[: args.limit]

    limiter = RateLimiter(state_path=RATELIMIT_STATE)
    console.print(
        f"\n[bold]{len(bases)}[/] base types to survey in [bold]{league}[/]"
        f"{f' at ilvl >= {args.ilvl}' if args.ilvl else ''}."
    )
    console.print(
        f"Cost: {len(bases)} searches and up to {len(bases)} fetches."
    )
    left = limiter.bucket("trade-search-request-limit").budget_remaining()
    if left:
        console.print(f"Search allowance left in the tightest window: [bold]{left[0]}[/]")
    console.print("[dim]The limiter will pace this; expect it to take a while.[/]")

    if not args.yes:
        if not console.input("\nProceed? [y/N] ").strip().lower().startswith("y"):
            console.print("Nothing spent.")
            return 0

    # The longest-waiting command in the repo - 39 bases against a 30-per-300s
    # search limit parks for minutes at a time, behind a frozen "(3/39) Opal
    # Ring...". This output is line-oriented rather than a spinner, so report
    # once per wait rather than every five seconds; `waited == 0` is the first
    # report of a given wait. Abandoning a survey that looks hung throws away
    # searches already spent, and searches are the scarcest thing here.
    limiter.on_wait = lambda w: (
        console.print(f"[dim]  {w.describe()}[/]") if w.waited == 0 else None
    )

    rows: list[dict] = []
    with TradeClient(league, limiter, status=args.status) as trade:
        trade.load_rates()  # so divine-priced bases convert to chaos
        for i, base in enumerate(bases, 1):
            console.print(f"[dim]({i}/{len(bases)})[/] {base}...")
            r = trade.base_value(base, ilvl_min=args.ilvl, influence=args.influence)
            rows.append(
                {
                    "base": base,
                    "listed": r.total,
                    "median": r.median,
                    "cheapest": r.cheapest,
                    "priced_sample": len(r.priced),
                    "error": r.error,
                }
            )
    limiter.save(force=True)

    ensure_dirs()
    slug = args.category or "custom"
    out = Path(args.out) if args.out else DATA_DIR / f"base-survey-{slug}.json"
    out.write_text(
        json.dumps(
            {
                "league": league,
                "ilvl_min": args.ilvl,
                "influence": args.influence,
                "status": args.status,
                "surveyed_at": time.time(),
                "bases": rows,
            },
            indent=2,
        )
    )

    table = Table(title=f"Unrolled base prices - {slug}", header_style="bold")
    table.add_column("base")
    table.add_column("listed", justify="right")
    table.add_column("priced", justify="right")
    table.add_column("cheapest", justify="right")
    table.add_column("median", justify="right")
    # Only samples that clear the confidence floor set the comparison point.
    # A median drawn from one or two listings is an anecdote, and one troll
    # listing moves it arbitrarily far - a 3-listing Topaz Ring priced 2c at
    # the cheap end reported a median of 14421c.
    confident = [r for r in rows if r["priced_sample"] >= MIN_CONFIDENT_SAMPLE]
    baseline = _median([r["median"] for r in confident])
    ranked = sorted(rows, key=lambda r: -(r["median"] or -1))
    for r in ranked:
        n = r["priced_sample"]
        if r["error"]:
            table.add_row(r["base"], "[red]error[/]", "", "", f"[dim]{r['error'][:40]}[/]")
        elif r["listed"] == 0:
            table.add_row(r["base"], "0", "0", "", "[dim]not sold as a base[/]")
        elif r["median"] is None:
            table.add_row(r["base"], str(r["listed"]), "0", "", "[dim]no convertible prices[/]")
        elif n < MIN_CONFIDENT_SAMPLE:
            table.add_row(
                r["base"],
                str(r["listed"]),
                f"[yellow]{n}[/]",
                f"{r['cheapest']:.0f}c",
                f"[dim]{r['median']:.0f}c (n={n}, not comparable)[/]",
            )
        else:
            table.add_row(
                r["base"],
                str(r["listed"]),
                str(n),
                f"{r['cheapest']:.0f}c",
                _price_cell(r["median"], baseline),
            )
    console.print(table)
    console.print(f"\nSaved to [bold]{out}[/]")

    thin = [r for r in rows if r["median"] is not None and r["priced_sample"] < MIN_CONFIDENT_SAMPLE]
    if thin:
        console.print(
            f"\n[yellow]{len(thin)} of {len([r for r in rows if r['median'] is not None])} "
            f"priced rows fall below the {MIN_CONFIDENT_SAMPLE}-listing confidence floor.[/]\n"
            "Their medians are shown but not ranked against the others, and are not used as\n"
            "the comparison point. The crafting-base market is genuinely thin, so this is\n"
            "normal - it means the numbers are directional, not measured."
        )
    console.print(
        "\n[dim]These are WHITE (unrolled) bases, so the price is the base itself rather\n"
        "than the mods on it. Measuring rares instead does not work - their price is\n"
        "dominated by their mods, which swamps the base entirely.\n"
        "'not sold as a base' is a real signal: nobody bothers to list it unrolled.\n"
        "Most crafting bases are influenced, so without --influence expect thin results.[/]"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="poescan",
        description="Flag potentially valuable rares sitting in your PoE1 dump tabs.",
    )
    p.add_argument("--version", action="version", version=f"poescan {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="configure client id, league and tabs")
    s.add_argument("--client-id", help="OAuth client id from my-account/applications")
    s.add_argument("--league", help='league name, e.g. "Allflame"')
    s.add_argument("--port", type=int, help="loopback port for the OAuth redirect")
    s.add_argument("--tabs", help="default tabs to scan, comma separated")
    s.add_argument("--account", help="full account name including discriminator, e.g. 'Name#1234'")
    s.add_argument(
        "--poesessid",
        help="session cookie, as a fallback when no OAuth client id is available",
    )
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("login", help="authorise via the browser")
    s.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("tabs", help="list your stash tabs")
    s.set_defaults(func=cmd_tabs)

    s = sub.add_parser("scan", help="scan tabs and report candidates")
    s.add_argument("--tabs", help="tabs to scan, comma separated (default: config, else all)")
    s.add_argument("--league", help="override the configured league")
    s.add_argument("--rules", help="path to an alternative ruleset YAML")
    s.add_argument("--promote-score", type=float, help="override the promotion threshold")
    s.add_argument("--max-checks", type=int, help="cap on trade API price checks")
    s.add_argument("--no-verify", action="store_true", help="skip the trade API entirely")
    s.add_argument("--fresh", action="store_true", help="ignore cached market checks")
    s.add_argument(
        "--status",
        default=DEFAULT_STATUS,
        choices=sorted(STATUS_OPTIONS),
        help="which listings to price against (default: securable = instant buyout)",
    )
    s.add_argument("--cache-hours", type=float, default=72.0, help="cached check lifetime")
    s.add_argument("--out", help="write the HTML report here")
    s.add_argument("--json", help="also write raw results as JSON")
    s.add_argument("--no-html", action="store_true", help="skip the HTML report")
    s.add_argument("--no-open", action="store_true", help="do not open the report in a browser")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("diagnose", help="free pre-flight check: what does triage see in your tabs?")
    s.add_argument("--tabs", help="tabs to inspect, comma separated")
    s.add_argument("--league", help="override the configured league")
    s.add_argument("--rules", help="path to a ruleset YAML")
    s.set_defaults(func=cmd_diagnose)

    s = sub.add_parser("validate-rules", help="check ruleset mod templates against the trade stats")
    s.add_argument("--rules", help="path to a ruleset YAML")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("categories", help="list category ids usable in rules")
    s.set_defaults(func=cmd_categories)

    s = sub.add_parser("budget", help="show remaining API allowance (makes no requests)")
    s.set_defaults(func=cmd_budget)

    s = sub.add_parser(
        "analyse", help="what do the accumulated market checks say? (makes no requests)"
    )
    s.add_argument("--league", help="override the configured league")
    s.add_argument("--rules", help="path to a ruleset YAML")
    s.add_argument(
        "--min-samples",
        type=int,
        default=1,
        help="hide rows backed by fewer items than this (default: 1)",
    )
    s.add_argument(
        "--tail",
        type=float,
        default=calibration.TAIL_THRESHOLD,
        help=(
            "chaos value above which an item counts as one worth catching "
            f"(default: {calibration.TAIL_THRESHOLD:g})"
        ),
    )
    s.add_argument(
        "--all-versions",
        action="store_true",
        help="pool observations written under other rulesets (their rule labels are stale)",
    )
    s.set_defaults(func=cmd_analyse)

    s = sub.add_parser(
        "base-values", help="what each base type is worth unrolled (poe.ninja aggregate)"
    )
    s.add_argument("--slot", help='item class, e.g. "Ring", "Body Armour"')
    s.add_argument("--slots", action="store_true", help="list the slots available and exit")
    s.add_argument(
        "--influences", action="store_true", help="list the influences available and exit"
    )
    s.add_argument("--influence", help='e.g. Shaper, "Shaper/Elder", or None for uninfluenced')
    s.add_argument("--ilvl", type=int, help="only rows at or above this item level")
    s.add_argument(
        "--min-samples",
        type=int,
        default=MIN_CONFIDENT_SAMPLE,
        help=f"hide rows backed by fewer listings (default: {MIN_CONFIDENT_SAMPLE})",
    )
    s.add_argument("--top", type=int, default=30, help="rows to show (default: 30)")
    s.add_argument("--league", help="override the configured league")
    s.add_argument("--refresh", action="store_true", help="ignore the 24h cache")
    s.set_defaults(func=cmd_base_values)

    s = sub.add_parser("survey-bases", help="spot-check a base against GGG's own trade API")
    s.add_argument("--category", help="category id to survey, e.g. accessory.ring")
    s.add_argument("--bases", help="explicit base types instead, comma separated")
    s.add_argument("--ilvl", type=int, help="only count listings at or above this item level")
    s.add_argument(
        "--influence",
        choices=["shaper", "elder", "crusader", "hunter", "redeemer", "warlord"],
        help="restrict to influenced bases - where the base is what is being sold",
    )
    s.add_argument("--limit", type=int, help="survey at most this many bases")
    s.add_argument("--league", help="override the configured league")
    s.add_argument(
        "--status",
        default=DEFAULT_STATUS,
        choices=sorted(STATUS_OPTIONS),
        help="which listings to price against (default: securable = instant buyout)",
    )
    s.add_argument("--out", help="write the survey JSON here")
    s.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    s.set_defaults(func=cmd_survey_bases)

    s = sub.add_parser(
        "dismiss", help="stop re-pricing items you have already judged not worth it"
    )
    s.add_argument(
        "item_ids",
        nargs="*",
        metavar="ITEM-ID",
        help="ids from the report - each card prints the command to copy",
    )
    s.add_argument("--note", help="why, recorded for your own future reference")
    s.add_argument("--league", help="override the configured league")
    s.add_argument("--list", action="store_true", help="show what is currently dismissed")
    s.add_argument("--undo", action="store_true", help="reverse the dismissal of these ids")
    s.set_defaults(func=cmd_dismiss)

    s = sub.add_parser("cache", help="inspect or clear the cache")
    s.add_argument("--clear", action="store_true", help="drop cached market checks")
    s.add_argument("--league", help="scope both the summary and --clear to one league")
    s.set_defaults(func=cmd_cache)

    s = sub.add_parser("refresh-data", help="re-download trade metadata")
    s.set_defaults(func=cmd_refresh_data)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
