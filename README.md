# POE1 Stash Scanner

Finds the rare items in your Path of Exile 1 dump tabs that are actually worth something, so you
don't have to wait for strangers to whisper you about them.

It reads your stash over the official API, scores every rare offline, then price-checks the
survivors against live trade listings. What you get back is a ranked shortlist with each item's
tab and grid position, what comparable items are selling for, and a link to the same search on the
trade site.

**It does not claim to price your items.** Rares are the one category poe.ninja deliberately
doesn't value, because it's genuinely hard. This tool decides which of the 300 rares in your dump
tab deserve thirty seconds of your attention. The final call stays yours.

```
Scanned: ~price 20 chaos (#16)
135 items / 130 rare / 24 flagged (24 market checks, 0 cached, 41s)

┏━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃     ┃ Item             ┃ Where           ┃ Market             ┃ Why                 ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│   1 │ Glyph Pendant    │ ~price 20 chaos │ 6 listed, med 65c  │ High global crit    │
│     │ Onyx Amulet      │ col 4, row 1    │                    │ multiplier; Large   │
│     │ Amulet ilvl 84   │                 │                    │ attribute total     │
│   2 │ Golem Grip       │ ~price 20 chaos │ 166 listed,        │ High life roll;     │
│     │ Two-Stone Ring   │ col 12, row 19  │ med 40c            │ Near top-tier life  │
│   3 │ Sol Collar       │ ~price 20 chaos │ 1184 listed,       │ High global crit    │
│     │ Agate Amulet     │ col 10, row 3   │ med 2c             │ multiplier          │
└─────┴──────────────────┴─────────────────┴────────────────────┴─────────────────────┘
```

Note row 3: it scored *highest* on the rules and is worth 2 chaos. That's the whole reason the
market check exists.

## How it works

Three stages, ordered by cost.

### 1. Parse

Reads your stash, then normalises every item — base type, item level, influence, and each mod
resolved to its trade stat id (`+97 to maximum Life` → `explicit.stat_3299347043`). It also
computes the pseudo-totals rares are actually priced on: total life including the half-life
granted by Strength, total elemental resistance counting `+X% to all Elemental Resistances` three
times, and so on.

The game and the trade site word the same mod differently in three systematic ways, all handled
here:

| your item says | trade calls it |
|---|---|
| `42% increased Armour and Energy Shield` | `#% increased Armour and Energy Shield **(Local)**` |
| `32% **reduced** Attribute Requirements` | `#% **increased** Attribute Requirements`, value negated |
| `You can apply **an additional Curse**` | `You can apply **# additional Curses**` |

A mod that fails to resolve is invisible to both scoring and search, so `poescan diagnose` reports
any it finds. On a real 367-item sample this sits at 0.1%.

### 2. Triage — free, offline, instant

Scores every item against a YAML ruleset. This stage exists because the trade API allows roughly
**30 searches per 5 minutes**, so querying every item in a quad tab is impossible. Triage discards
the obvious nothing — about 73% of rares — so the API budget goes to the rest.

Crucially, **triage is a filter, not a ranking.** Measured against a real dump tab, the score had
no useful correlation with price: the highest-scoring item was worth 2c, while items scoring half
that fetched 25–65c. So the threshold is set low and the market does the deciding.

### 3. Market verification

Builds a trade search from the item's own rolls and reads back the cheapest listings. The useful
signal is the pair *(how many are listed, what do they ask)*:

| result | meaning |
|---|---|
| 9000 listed, median 4c | junk — ranked below everything |
| 21 listed, median 20c | worth a look |
| 6 listed, median 65c | worth a look now |

Queries are refined in **both** directions. Zero results means the query was over-specified — which
happens most for exactly the items worth pricing — so it widens to the single strongest mod. Over
300 results means the cheapest listings describe the market floor rather than your item, so it
narrows using more of the item's mods.

## What makes the numbers trustworthy

These were measured against the live API, not assumed. They are the difference between a useful
tool and a plausible-looking one.

**Instant buyout only.** The trade site's `status` filter splits the market in half, and the halves
disagree wildly. The same wand:

| listing type | listings | median |
|---|---|---|
| Instant Buyout (`securable`) | 4126 | **90c** |
| In Person (`online`) | 54 | **4c** |

The cheap in-person listings are largely bait that never sells. Instant buyout is both the truer
price and the far larger, more stable sample. Pricing against the wrong half is a 20× error.

**A handful of expensive listings is not evidence.** Three unsold listings at 4 divine mean *those
didn't sell*, not that yours is worth 4 divine. Results below five priced comparables are damped in
ranking and flagged in the report.

**Magnitude is not importance.** An early version filled search filters by roll size, which put
`+54 Accuracy Rating` above mods that matter and "valued" a 2c amulet at 570c against three
coincidental matches. Stats nobody buys an item *for* are now excluded when narrowing a search.

**Unpriced listings are excluded.** Items listed without a price have to be haggled over in
whisper — they inflate the competition count while contributing nothing to a median.

## Install

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:tommeng/POE1-Stash-Scanner.git
cd POE1-Stash-Scanner
uv sync
```

## Connect your account

Two options. Try OAuth first.

### Option A — OAuth (preferred)

Register a client at [pathofexile.com/my-account/applications](https://www.pathofexile.com/my-account/applications):

- **Application type:** Public
- **Redirect URI:** `http://127.0.0.1:8888/callback`
- **Scopes:** `account:stashes`

```bash
uv run poescan setup --client-id <your-client-id> --league Allflame
uv run poescan login      # opens a browser once
```

Tokens are stored in `~/.poescan/token.json` (mode 600) and refresh automatically for 7 days.

> **Heads up:** GGG's developer docs currently state *"We are currently unable to process new
> applications."* Registration is manual, via `oauth@grindinggear.com`. If your account has no
> existing client and you can't create one, use Option B.

### Option B — session cookie

Log in to pathofexile.com, then **DevTools → Application → Cookies → `https://www.pathofexile.com`**
and copy the value of the row named exactly `POESESSID`. It is 32 hexadecimal characters — if what
you copied is longer, you've grabbed `cf_clearance`, which sits next to it.

```bash
uv run poescan setup --account 'YourName#1234' --poesessid <cookie> --league Allflame
```

The account name must include the `#1234` discriminator. The cookie is stored in
`~/.poescan/config.json` (mode 600), is never printed or logged, and is only ever sent to
pathofexile.com — but it *is* full account access, so treat it like a password. GGG discourage
giving it to third-party apps; that warning targets remote services and this tool is local, but it
is still the weaker of the two options.

Either way, confirm it works:

```bash
uv run poescan tabs
```

## Usage

```bash
# Free pre-flight: reads your stash, makes ZERO trade API calls
uv run poescan diagnose --tabs "dump1,dump2"

# The real thing
uv run poescan scan --tabs "dump1,dump2"
```

`scan` prints the ranked table and opens an HTML report showing, per item: which tab and grid
position it's in, why it was flagged, its full mod list, comparable listing prices, and a link that
opens the same search on the trade site.

Run `diagnose` first after any ruleset change — it costs nothing and reports unrecognised item
categories, unresolved mods, the score distribution against your threshold, and which rules fired.

### Selecting tabs

Tab names are often bare numbers and are not unique, so matching is precise rather than helpful:

```bash
uv run poescan scan --tabs "Misc,To sell"   # exact names (may match several)
uv run poescan scan --tabs "#20,#33"        # by index — the only way to separate duplicates
uv run poescan scan                          # every tab that can hold gear
```

Special tabs (Currency, Maps, Gems, Flasks…) can't hold rare gear and are skipped automatically.

### Flags

| flag | effect |
|---|---|
| `--no-verify` | triage only, zero API calls — instant |
| `--max-checks N` | cap trade queries this run |
| `--promote-score N` | override the threshold without editing the ruleset |
| `--status X` | which listings to price against (default `securable`) |
| `--fresh` | ignore cached market checks |
| `--json out.json` | machine-readable results alongside the HTML |

Market checks are cached in SQLite for 72 hours keyed on item id, so rescanning a tab you've
already looked at costs almost nothing. Failed checks are deliberately *not* cached, so a dropped
connection doesn't poison the results until the cache expires.

### Other commands

```bash
uv run poescan analyse       # what your accumulated market checks say about the ruleset
uv run poescan budget        # remaining API allowance, from saved state
uv run poescan cache         # how much is cached
uv run poescan refresh-data  # re-download trade metadata after a league or patch
uv run poescan survey-bases --category accessory.ring --ilvl 84 --influence shaper
```

`analyse` and `budget` make no requests at all. `survey-bases` measures what an unrolled base type
sells for, one search per base — see [Tuning](#tuning) for what it's good for and what it isn't.

## Rate limits

Exceeding a GGG rate limit earns a **timed ban**, not a soft rejection, and the ban scales with
which window you broke — 60s for the short one, up to 30 minutes for a longer one. So the client
never probes the limit and backs off; it throttles *before* each request so it never hits it at all.

Three things make that work:

- **Limits come from GGG, not from constants.** Every response carries `X-Rate-Limit` headers giving
  both the rules and your current usage. Those are parsed and obeyed, so a change on GGG's side is
  picked up automatically. One request of headroom is reserved in every window.
- **All published rule sets are honoured, not just the first.** The stash endpoint enforces an
  account limit *and* an IP limit simultaneously, and they differ (30/min vs 45/min). They're merged
  per window, keeping the tightest allowance and the most pessimistic reported usage.
- **Usage persists between runs.** Limits are per-account and per-IP, not per-process, so state is
  saved to `~/.poescan/ratelimit.json`. Without that, running two scans back to back would have the
  second one start blind and spend a budget the first already used — which is exactly how you'd earn
  a ban. An active ban survives the process that earned it, too.

Check where you stand at any time. This reads saved state and makes no requests:

```bash
uv run poescan budget
```

```
                API allowance remaining
┏━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━┳━━━━━━┳━━━━━━━━━━━━━┓
┃ policy         ┃ window ┃ used ┃ left ┃             ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━╇━━━━━━╇━━━━━━━━━━━━━┩
│ trade-search   │    10s │    2 │    2 │ ##########  │
│ trade-search   │   300s │   13 │   16 │ ########    │
│ trade-search   │     6h │   34 │  565 │ #           │
└────────────────┴────────┴──────┴──────┴─────────────┘
```

Caching helps too: market checks are stored for 72 hours keyed on item id, so rescanning a tab you
have already looked at spends almost nothing. `--no-verify` skips the trade API entirely, and
`diagnose` never touches it.

## Tuning

`poescan/rules/default.yaml` is the domain knowledge, and it's meant to be edited — the PoE meta
moves and a static ruleset goes stale. Mod strings are *templates*: the rolled numbers replaced by
`#`, exactly as the trade site writes them.

```yaml
- id: my-pet-mod
  score: 28
  note: "Why this matters"
  categories: [armour.boots]
  all:
    - {mod: "#% increased Movement Speed", min: 35}
```

Condition types: `pseudo` (computed totals), `mod`/`mod_any` (exact template), `mod_matches`
(regex, tested against both rolled text and template), `stat`/`stat_any` (trade stat id), `flag`,
`base`/`base_any` (exact base type) and `base_matches` (regex, for families), `category`, and the
scalars `ilvl`, `links`, `sockets`, `quality`, `influence_count`, `mod_count`. Add `abs: true` for
mods that roll negative, like `-7 to Total Mana Cost of Skills`.

Base type is much finer than `category` — an Opal Ring and an Iron Ring are both `accessory.ring`.
Note that base value is conditional rather than intrinsic: seven of the ten known-1-chaos reference
rings are ilvl 83–85 Two-Stone Rings, a base most guides call desirable. So a `base` condition
almost always belongs alongside an `ilvl` or mod condition rather than scoring on its own.

```bash
uv run poescan validate-rules   # every mod string, regex, flag, pseudo field, base and category
uv run poescan categories       # valid category ids
```

`validate-rules` matters more than it sounds. Every condition kind fails *closed*: a typo in a flag
name or a mod template doesn't error, the rule just silently never fires, forever. Run it after
every edit.

Two warnings, both learned by measurement:

- **Resist the urge to raise `promote_score` when results feel noisy.** That reintroduces false
  negatives — the failure mode that actually costs you money — and the market check already sorts
  the noise to the bottom.
- **Don't write nested rules that stack.** A rule for `life >= 90` scoring 7 plus one for
  `life >= 115` scoring another 5 looks like two signals, but the second can never fire without the
  first, so `analyse` can never tell them apart and will report identical numbers for both no matter
  what they're worth. Write disjoint bands instead — `none: [{pseudo: life, min: 115}]` on the lower
  one — so each describes a different population.

### Checking whether your rules earn their score

Every scan pays for real price observations, and records the item that produced each one. `analyse`
reads them back — offline, no API calls:

```bash
uv run poescan analyse
```

It reports whether the triage score tracks price at all, the price distribution of the items each
rule fired on, which rules never fire, and the base types you actually own. That's the evidence to
edit the ruleset from, rather than intuition. It needs a few scans' worth of data before the
per-rule numbers mean much — five or more observations per row is the point where a median stops
being an anecdote.

## Limitations

- **Listed prices are asking prices, not sale prices.** They skew high, and stale or price-fixed
  listings are common. Treat the numbers as a relative signal.
- **The ruleset encodes a snapshot of the meta** and will need occasional edits.
- **Triage can miss niche items.** A mod combination that's worthless to most builds but perfect
  for one is exactly the case hand-written rules don't catch.
- **Rate limits are real.** Roughly 30 searches per 5 minutes and 600 per 6 hours, so large scans
  take minutes rather than seconds. See below for how they're handled.

## Development

```bash
uv run pytest                             # 243 tests, ~3.7s
uv run pytest tests/test_ratelimit.py     # one file
uv run pytest -k test_reduced_resolves    # one test
uvx ruff check poescan --select F,E9
```

The suite includes ten rings scraped from live trade, all listed at 1 chaos, asserted to stay below
genuinely valuable items — the regression guard against the ruleset drifting loose. Rate-limit
behaviour is tested against an injected clock rather than by sleeping.

**The suite never touches the network.** Trade metadata is vendored at `tests/data/*.json.gz`, and a
session-wide fixture disables the real HTTP transport, so a test that tries to reach GGG fails
loudly instead of quietly spending your API budget. Both of those are there because the suite once
did exactly that — roughly four live requests per run, which eventually made it stall for five
minutes when the allowance ran out.

`CLAUDE.md` documents the architecture in more depth, including the non-obvious parts worth knowing
before changing scoring or the rate limiter.

## License

[MIT](LICENSE).

Not affiliated with or endorsed by Grinding Gear Games. Path of Exile is their trademark. This
tool only reads your own stash and the public trade API, and self-throttles against GGG's
published rate limits.
