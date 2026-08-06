# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                   # install
uv run pytest                             # 294 tests, ~3.9s (fully offline)
uv run pytest tests/test_ratelimit.py     # one file
uv run pytest -k test_reduced_resolves    # one test by name
uv run poescan validate-rules             # every mod string and base name in the ruleset really exists
uv run poescan budget                     # remaining API allowance; reads saved state, makes no requests
uv run poescan analyse                    # what the accumulated checks say; makes no requests
uv run poescan base-values --slot Ring --influence Shaper --ilvl 84   # poe.ninja aggregate
uv run poescan survey-bases --category accessory.ring --ilvl 84 --influence shaper  # GGG, spot-check
uvx ruff check poescan --select F,E9      # ruff is not configured in pyproject; invoke it explicitly
```

`uv run poescan diagnose --tabs "#16"` is the cheap way to see what the pipeline makes of a real
stash: it reads items but makes **zero** trade API calls. Run it before any change that touches
parsing or rules. `uv run poescan scan --no-verify` likewise skips the network entirely.

## What this is

Scans rare items in Path of Exile 1 dump tabs and ranks the ones worth a second look. It
deliberately does **not** claim to price items — it decides what is worth a human glance.
See `README.md` for user-facing setup.

## Architecture

Three stages, ordered by cost. The shape is forced by the trade API's rate limits
(~30 searches / 5 min, 600 / 6 h), which make one API call per item impossible for a 600-slot tab.

1. **Parse** (`stash.py` → `items.py` → `pseudo.py`). Stash JSON becomes a normalised `Item`:
   category, ilvl, influence, and mods each resolved to a trade stat id. `pseudo.py` computes the
   aggregates rares are actually priced on (total life including half-life from Strength, total
   elemental resistance counting `all Elemental Resistances` three times).
2. **Triage** (`triage.py` + `rules/default.yaml`). Free, offline scoring. Discards the obvious
   nothing — roughly 73% of rares — so the API budget goes to the rest.
3. **Market verification** (`trade.py`). Builds a trade search from the item's own rolls and reads
   back the cheapest listings.

`scanner.py` orchestrates and ranks; `report.py` renders HTML.

### Triage scores are a filter, not a ranking

This is the single most important thing to understand before changing scoring.

Measured against a real dump tab, triage score had **no useful correlation with price**: the
highest-scoring item (31) was worth 2c, while items scoring 12 ranged from 1c to 25c. What did
track value was scarcity — 5–21 comparable listings meant 20–65c, while 500+ meant 1–2c.

So `promote_score` is deliberately low (10), and `Candidate.interest` in `scanner.py` is dominated
by the market result, with the triage score contributing only a small tiebreak. Unchecked items
fall back to their triage score, which lands them mid-pack — ahead of confirmed junk, behind
confirmed value.

Do not "fix" this by raising `promote_score` to reduce noise. Doing that reintroduces false
negatives, which is the failure mode that costs the user money. The relevant regression tests are
`test_market_outranks_triage_score` and `test_scan_surfaces_every_valuable_item`.

### Empirically established constants

These were measured against the live API, not reasoned about. Changing them needs new measurement,
not argument.

- **`status: securable` (Instant Buyout) is the default and matters enormously.** On one wand:
  `securable` gave 4126 listings / 90c median; `online` (In Person) gave 54 / 4c. The cheap
  in-person listings are largely bait that never sells. The interest thresholds in `scanner.py`
  are anchored to instant-buyout prices and would need retuning if `--status` changes.
- **`sale_type: priced`** excludes listings with no price (they must be haggled over in whisper, so
  they inflate the competition count while contributing nothing to a median).
- **Confidence floor of 5 priced listings** (`MIN_CONFIDENT_SAMPLE`). Three unsold listings at
  4 divine say "these did not sell", not "yours is worth 4 divine". High medians from thin samples
  are damped in ranking and flagged in the report. The *junk* signal is deliberately not damped —
  "thousands listed at 1c" is never a small-sample artifact.
- **Queries are refined in both directions.** Zero results → widen to the single strongest mod
  (a good item genuinely has no exact peers). Over 300 results → narrow using more of the item's
  mods, because the cheapest of 8000 listings is the market floor, not a comparable.
- **`LOW_SIGNAL_MOD` exists because magnitude is not importance.** Filling filter slots by roll size
  put `+54 Accuracy Rating` above real mods and "priced" a 2c amulet at 570c against three
  coincidental matches. Accuracy, light radius, mana regen, hybrid attributes etc. are excluded
  when narrowing.

### Calibration: measuring instead of guessing

The 49 rule scores in `default.yaml` are hand-authored priors. Nothing measured them, and the
repo's own finding that triage score doesn't predict price is what unmeasured priors look like.
Two commands exist to replace them with evidence.

**`analyse`** reads `market_check` back. Every scan already pays for real price observations, and
`scanner._features` now records the item that produced each one — base, ilvl, flags, mods,
`rules_hit`. `analyse` reports, per rule and per base, the price distribution of the items it
fired on. Free, offline, and it compounds with every scan. Checks written before feature logging
carry no item side; `Cache.label_features` labels them on the next cache hit, so one ordinary
scan over the same tabs retro-labels the whole cache for zero API calls.

Two properties of that instrument are load-bearing, and it misled for months without them:

**Rules are judged on their tail, not their median.** Triage is a filter, and a filter is judged by
what it wrongly discards — a different question from what its typical item is worth, with a
different answer. Of the 16 observations worth 50c+, **12 scored in the lowest band**, and the most
valuable item in the set (820c) scored exactly `promote_score`. So every table in `calibration.py`
reports `n`, `median`, `best` and `>=50c` together and ranks on tail count. `analyse` also prints
what raising `promote_score` would cost, as a measured number.

**A rule id means nothing without the ruleset that defined it.** `triage.fingerprint` hashes the
`rules`, `veto`, `promote_score` and `min_ilvl` (not `max_market_checks` — a budget knob must not
orphan evidence), and every observation is stamped with it. `analyse` sets aside observations from
other rulesets by default and says so; `--all-versions` pools them with a warning. `label_features`
re-labels a stale row on a cache hit, keeping the price and refreshing the explanation, so a rule
edit no longer strands everything measured before it. The maths is in `calibration.py`, separate
from the command that renders it, and tested — it is the instrument the scores get corrected by.

**`survey-bases`** measures what a base is worth on its own. It queries **white (normal rarity)
influenced** bases, and that specific choice is the entire finding — four other designs were
built and killed against the live API, each ranking Sapphire and Iron Rings *above* Opal:

| design | why it failed |
|---|---|
| floor price of rares | sorting ascending across a saturated market finds the 1c market floor, identical for every base |
| share of rares above a price | the denominator saturates at the API's 10000 result cap, inflating popular bases |
| price leaving a fixed count | a fixed count is not a fixed quantile — top-300 of 50000 listings is the 99th percentile, of 1700 the 83rd |
| price leaving a fixed fraction | removes that error and still inverts |

All four measured **rare** items, whose price is dominated by their mods; the base is a sliver of
the variance and gets swamped. A white base has no mods, so its price *is* the base.
`--influence` matters, since the crafting-base market is almost entirely influenced items.

**But the survey's samples are far too thin to calibrate from** — 39 ring bases yielded exactly one
row clearing the five-listing floor. `ninja.py` imports the same measurement from poe.ninja's
economy API instead: one request, 19.4k rows across 23 slots, ~853 bases with a usable sample.
The two agree on ordering (Vermillion > Opal > Amethyst ≈ Two-Stone > Sapphire ≈ Iron), which is
worth something as mutual validation, but poe.ninja's magnitudes run lower and its counts are
10–100× larger. It is the same underlying stash data, better aggregated — not a second opinion.

The survey's "not sold as a base" rows were an artifact of that thinness, not signal: Sapphire and
Iron Rings showed n=0 live while poe.ninja samples 385 and 152 of them at 1c. Note that
poe.ninja's `count` is a **capped sample** (it saturates at 399), not a listing count —
`ninja.py` exposes it as `samples` with `listings` alongside, because reading one as the other
reintroduces exactly the saturating-denominator error that killed the survey's third design. A zero from a thin
sample is still a thin sample. `TradeClient.base_value` is kept as the independent spot-check.

### Blocked on measurement — do not act on these yet

Discrete work is tracked in GitHub issues. This section is for the opposite: things
that look actionable and are not, because the evidence is not there yet. Each has
already tempted someone once.

- **~~`fractured` scores 16 and its items median 1c.~~** *Resolved by measuring it
  correctly rather than by more data.* The seven observations are
  `1c, 1c, 1c, 1c, 4.5c, 100c, 202.5c` — median 1c, but two of seven clear 50c
  against a 5c baseline. It was never short of evidence; a median column simply
  cannot see what the decision turns on. Keep the rule. The open question is now
  the narrower one: whether qualifying by *what* was fractured beats the flat 16.
  Note the general lesson, which applies to several entries below — check the
  statistic before concluding the sample is thin.
- **Base-type rules are not yet justified.** `base-values` gives good per-base numbers,
  but it prices *crafting bases* — white influenced items at ilvl 82–86. Triage asks a
  different question: does this base make a **rolled rare** worth more. Nothing measures
  that yet. The feature log is the instrument that could, once bases reach n≥5; 34 sit
  at n=1 today.
- **The resistance bands have never fired on a priced item.** `high-total-res` and
  `huge-total-res` were split into disjoint bands so they *can* be measured, which is
  not the same as having been measured. Their scores remain unevidenced priors.
- **`attribute-stacking`'s threshold of 70 is the only thing promoting a multi-divine amulet.**
  The item in `TrainingData/Hypnotic Beads, Turquoise Amulet/` scores exactly `promote_score` on
  that rule alone — 76 total attributes against a min of 70 — while its 84% total resistance misses
  `high-total-res`'s 130. A margin of zero on a 1–10 divine item invites moving the threshold down,
  and one item is not evidence that 70 is wrong. n=1, and it is pinned by a test instead.
- **Six ring bases return HTTP 503 from GGG consistently** — Dusk, Penumbra, Gloam,
  Tenebrous, Shadowed, Nameless. Identical on retry, and no other base does it. Most
  likely they do not exist in the current league and the API answers 503 rather than
  a proper error. Surfaced honestly rather than guessed at.

The general rule this section exists to enforce: **a thin sample is thin in both
directions.** A median of 1c over seven items is no more conclusive than a median of
500c over two, and a *zero* is no exception — the base survey reported Sapphire and Iron
Rings as "not sold as a base" on n=0, while poe.ninja samples 385 and 152 of them.

### Mod text ↔ trade stat translation (`tradedata.py`)

The game and the trade site word the same mod differently, in three systematic ways. All are
handled in `_candidates()`, and all were found by running `diagnose` against a real stash
(unresolved mods went from 4.3% to 0.1%):

| game text | trade text |
|---|---|
| `42% increased Armour and Energy Shield` | `#% increased Armour and Energy Shield **(Local)**` |
| `32% **reduced** Attribute Requirements` | `#% **increased** Attribute Requirements`, value negated |
| `You can apply **an additional Curse**` | `You can apply **# additional Curses**` |

`StatIndex.resolve()` returns `(stat_id, sign)`. **The sign is load-bearing** — a `reduced` roll
must be stored negative or every threshold comparison silently inverts. `lookup()` is the
convenience wrapper that discards it; prefer `resolve()` in new code.

A mod that resolves to no stat id is invisible to both triage and trade search, so `diagnose`
reports them. `Has N Abyssal Socket` is correctly unresolvable (a socket property, not a stat).

### Rate limiting (`ratelimit.py`)

Driven entirely by GGG's own `X-Rate-Limit` headers rather than hardcoded limits, because GGG
changes them without notice. Rules are `hits:period:penalty`; exceeding one earns a **timed ban**,
not a soft 429, so every bucket keeps one hit of headroom.

Two things beyond the obvious:

**All published rule sets bind at once.** The stash endpoint returns both `x-rate-limit-account`
(30:60:60) and `x-rate-limit-ip` (45:60:120). Honouring only one would allow 44 requests/min against
a 30/min cap. `_merge_policies` merges them per period, keeping the tightest rule and the most
pessimistic usage, which may come from different sets.

**State persists to `~/.poescan/ratelimit.json`.** Limits are per-account/per-IP, not per-process, so
a fresh `RateLimiter` would otherwise fire its first request blind — running two scans back to back
was a genuine ban risk. Timestamps are stored as wall-clock and translated back into monotonic time
on load; anything already outside the longest window is dropped. Writes are atomic via a temp file.

The subtle part is `sync_from_headers`. A long window's count includes hits that are long past;
stamping them all at `now` makes a 6-hour count of 32 look like 32 hits in the last 10 seconds and
stalls the client for a full 300s. Backfilled hits are therefore placed just outside the previous
(shorter) rule's window. `test_long_window_usage_does_not_stall_short_windows` pins this.

`RateLimiter` takes injectable `clock` and `sleeper` so time-dependent behaviour is tested without
real sleeps.

**A silent wait is indistinguishable from a hang**, and waiting out the 100-per-1800s stash window
legitimately takes half an hour. `wait()` therefore reports a `WaitStatus` before each 5-second nap
via `RateLimiter.on_wait`, so the wait explains itself while it happens:

```
Reading tab '26' (#44) - waiting 28m22s for rate limit (99 requests counted in the last 30m, limit 100)
```

`Bucket.binding()` returns the rule responsible, not just the delay — the rule is what turns a wait
into an explanation. A wait caused by a server ban says so instead, because no local count explains
one. The callback lives on the limiter rather than being threaded through `wait()`'s six call sites
because the limiter is already the one object shared by the stash and trade clients, neither of
which has any other use for a progress callback; it is expected to be scoped to one operation,
since nothing clears it. `scan()` composes it with the last thing `say()` announced; `tabs` and
`diagnose` do the same against their spinners, and `survey-bases` prints once per wait rather than
every five seconds because its output is line-oriented.

**`used` is counted against GGG's advertised limit and can legitimately exceed it.** Limits are
per-account *and* per-IP, so a shared IP, a VPN or a trade overlay put hits on the clock this
process never made, and `_merge_policies` keeps the tightest rule beside the most pessimistic count
— which come from different rule sets in exactly that case. Phrasing it as a fraction of our own
headroom-reduced budget rendered it `40/29 used`, which reads as having blown a limit: the one
thing that earns a ban, and the one thing that had not happened. Hence "counted in the last", which
says whose count it is without claiming it is all ours.

### Auth: two paths (`auth.py`, `stash.py`, `Config.auth_mode`)

- **OAuth** — public client, PKCE, loopback redirect. Preferred, but GGG's developer docs currently
  state *"We are currently unable to process new applications"*, so a client id may be unobtainable.
- **POESESSID** — `LegacyStashClient` against `character-window/get-stash-items`. Needs a
  browser-like User-Agent (Cloudflare rejects bot agents), the account name with `#` percent-encoded,
  and `realm=pc`. Its tab objects use short field names (`n`, `i`) unlike the OAuth API.

`scanner.open_stash()` picks between them from `Config.auth_mode`; everything downstream is
identical. Credentials are validated at setup time (`config.poesessid_problem`) because the
failure mode otherwise is an opaque 403 — a POESESSID is exactly 32 hex characters, and people
routinely paste `cf_clearance` instead. Never print the cookie; `setup` prints `set`, not the value.

### Tab selection

Tab names are frequently bare numbers and are **not unique** (real stashes have two tabs named
`2`). `select_tabs` therefore prefers exact name match, supports `#20` for index, and only falls
back to substring when nothing matched exactly. Special tabs (Currency, Maps, Gems…) cannot hold
rare gear and are excluded unless named explicitly.

## Tests

- `tests/data/real_rings.json` holds ten rings scraped from live trade, all listed at 1 chaos.
  They are the regression guard against the ruleset drifting loose. Re-run after any rule edit.
- **`F.HYPNOTIC_BEADS_AMULET` is the guard in the other direction** — against the ruleset drifting
  *tight*. A real amulet whose comparables ran from 20 chaos to 10 divine, and which clears
  `promote_score` by a margin of exactly zero. If a rule edit makes
  `test_a_known_multi_divine_amulet_is_still_promoted` fail, the edit has introduced a false
  negative on a multi-divine item. Transcribed in `TrainingData/`; the two are meant to stay in
  step. Only promotion is asserted, not the score — the score is an observation about a
  hand-authored prior, not something the ruleset owes.
- **Trade metadata is vendored** at `tests/data/{stats,items}.json.gz` and the `index` fixture
  reads it from there. It used to read the developer's own `~/.poescan/data` and `skip` when
  cold, so a fresh machine reported green while silently skipping two thirds of the suite.
  Refresh with `poescan refresh-data` then re-gzip into `tests/data/`.
- **No test may reach the live trade API.** The endpoints ban rather than soft-fail, so a suite
  that calls them spends a budget the user needs and eventually blocks on it — `test_trade.py`
  once leaked ~4 real fetches per run this way, because only `_search` was stubbed and
  `price_check` fetches listings whenever a search returns ids.
  A session-wide `autouse` fixture in `conftest.py` disables `httpx.HTTPTransport`, so this is
  enforced for every test rather than asserted by one. `httpx.MockTransport` still works.
  Tests that run `scan()` must also point `RATELIMIT_STATE` at a tmp path, or they rewrite the
  user's real budget accounting.
- Fixtures in `tests/fixtures.py` mirror the real stash JSON shape, so they exercise the same
  parsing path as live data.

## Editing the ruleset

`poescan/rules/default.yaml` is the domain knowledge and is meant to be edited as the meta moves.
Mod strings are *templates* — rolled numbers replaced by `#`, exactly as the trade site writes them.
`mod_matches` is tested against both the rolled text and the template, so patterns can be written
either way. Add `abs: true` for mods that roll negative (`-7 to Total Mana Cost of Skills`).
Always run `poescan validate-rules` after editing.

**One condition names one selector.** `{base: "Opal Ring", ilvl: {min: 84}}` is refused when the
ruleset loads — write the two as separate entries under `all`, which are ANDed anyway.
`evaluate_condition` tests selector kinds in a fixed order and returns on the first one present, so
the form used to evaluate the ilvl and discard the base silently. Rejecting rather than ANDing is
deliberate: `min`/`max` qualify *whichever* selector is present, so `{pseudo: life, mod: X, min: 90}`
has no single defensible reading. Aliases of one selector may still share a condition (`mod`
beside `mod_any`), because `evaluate_condition` genuinely unions those.
