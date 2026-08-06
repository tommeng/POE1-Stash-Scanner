# TrainingData

One folder per item. Screenshots are the provenance; `item.yaml` is the data.

## Why this exists

The repo's calibration instrument is `analyse`, which reads item features back out of
`market_check.payload` in the SQLite cache. That cache can only ever hold items the scanner
**promoted** — an item has to clear `promote_score` before anything spends an API call on it, and
nothing spends an API call on a rejection. So the cache is structurally blind to false negatives,
and a false negative is the failure mode that costs the user money.

This folder is the only place a rejection can be recorded. That is its point. A screenshot alone
does not achieve it: a PNG joins to nothing and cannot be queried, so the transcription in
`item.yaml` is the artifact and the image is what lets a human check the transcription.

Two items is not a dataset. Nothing here is wired into `analyse` or `cache.py` and nothing should
be until there are enough entries to answer a question — see "Blocked on measurement" in
`CLAUDE.md`.

## Format

`item.yaml`, four blocks.

**`source`** — where the observation came from, and specifically whether it came from poescan.
`independent_of_poescan: false` means the numbers are the tool restating its own conclusion; that
is circular and must never be counted as evidence *about* the tool. It is still worth keeping as a
regression case. The flag is a boolean rather than prose so it can be filtered on without reading.
Also carries `observed_on` and `league`, because both a price and a base's worth move between
leagues and within one.

**`item`** — field names mirror `scanner._features`: `base_type`, `category`, `ilvl`,
`influences`, `corrupted`, `fractured`, `synthesised`, and `mods` in the same `{t, id, v, d}`
shape (template, trade stat id, rolled value, domain). Deliberately the same vocabulary, so a
sidecar and a cache row can be compared without a translation layer. `id` is optional — it is
whatever `StatIndex.resolve()` returns and can be recomputed. Anything the provenance does not
actually show (sockets, links, quality on a report screenshot) is **omitted, not guessed**.

**`triage`** — `promoted`, `score`, the `promote_score` in force, and `rules_hit`. Derive these by
running the item through `triage.assess`; do not hand-calculate them. This block is the whole
reason the artifact is worth writing, and it is only true of the ruleset that produced it, so it
records `ruleset` and `scored_on` and should be re-derived after rule edits.

**`market`** — `total`, `filters` and `listings`, mirroring the cache's `market_check` payload.
Listing prices are raw `{amount, currency}` pairs and are **never converted to chaos**. The
divine:chaos rate moves within a league and across leagues, so a chaos figure baked in today
silently corrupts every later comparison; `trade.Listing.chaos_value` is null for every non-chaos
currency for exactly this reason. Compute a chaos figure at read time from a rate recorded
alongside the observation date, if one is ever wanted. `cheapest` and `median` appear only where
poescan itself computed them, and are chaos over the chaos-priced subset. `listings: null` means
the individual listings were not captured — an empty list would read as "there were none", which
is a different and much stronger claim.

## Adding an item

1. Make a folder named `<item name>, <base type>` and drop the screenshots in. Watch for
   zero-width characters when pasting a name out of the game or the trade site; one folder here
   ended in a U+200C and it is invisible in every tool that would show it to you.
2. Transcribe the item into `item.yaml`. Record only what the screenshots show.
3. Derive the `triage` block by running the item through `triage.assess` rather than reasoning
   about the ruleset. If the code disagrees with your arithmetic, the code is right.
4. If the item is valuable and triage **rejected** it, say so plainly in the `triage` note. That
   is the observation this folder exists to hold, and the cache cannot hold it.
