# Data pipeline: downloading the Congressional Record

A small, resumable pipeline that downloads **speech/section-level transcripts** from the
U.S. **Congressional Record** (the daily record of proceedings in the House and Senate) and
stores each as a plain-text file plus structured metadata.

Data comes from the authoritative federal source: the **GovInfo API** (`CREC` collection),
operated by the U.S. Government Publishing Office.

## What you get

For every "granule" (an individual speech, vote, prayer, or section within a day's Record):

```
data/
├── raw/<year>/<packageId>/<granuleId>.txt        # the transcript text
├── raw/<year>/<packageId>/<granuleId>.mods.xml   # original MODS metadata
└── manifest.jsonl                                # one JSON row per granule (index)
```

Each `manifest.jsonl` row includes: `granuleId`, `packageId`, `granuleClass`
(`HOUSE` / `SENATE` / `EXTENSIONS` / `DAILYDIGEST` / `FRONTMATTER`), `title`, `dateIssued`,
`congress`, `session`, `chamber`, the Congressional Record `citation`, the list of
`member_names` and `bioguide_ids` mentioned, character count, and the relative file paths.
Rows recovered from files already on disk (when the manifest was lost) carry the same fields
plus `"backfilled": true`.

## Coverage

The GovInfo `CREC` digital collection runs from **1994-01-01 to the present**. (Earlier years
exist only in the scanned *Bound* Congressional Record and are not covered here.) This pipeline
collects **all sections** of the daily Record by default — `HOUSE`, `SENATE`, `EXTENSIONS`,
`DAILYDIGEST`, and `FRONTMATTER` (narrow with `--classes`).

### Two ingest paths — check coverage before you trust a date

The repository fills its corpus through **two independent pipelines that keep separate
bookkeeping**. Reading either one alone will give you the wrong answer about how current
the data is:

| Path | Driver | Bookkeeping | Role |
| --- | --- | --- | --- |
| GovInfo **API** | `fetch_crec.py` | `data/manifest.jsonl` (+ transient `data/manifest_w*.jsonl` worker shards) | Granule text + MODS on disk |
| GovInfo **bulk** | `scripts/bulk_pipeline.py` | `data/interim/turns/*.parquet` | **The corpus the analysis code actually reads** |

`data/manifest.jsonl` is *not* the source of truth for coverage — the bulk turn Parquets are.
Always start with:

```bash
.venv/bin/python scripts/coverage_status.py
```

It prints the true date range of every ingest path, writes `data/coverage_status.json`, and
warns when:

* worker shards (`data/manifest_w*.jsonl`) hold granules missing from `data/manifest.jsonl`
  → fix with `.venv/bin/python scripts/merge_manifests.py`;
* `data/manifest.jsonl` contains duplicate `granuleId` rows → same fix;
* `data/manifest.jsonl` has **interior holes** — days inside its own date range that the
  analysis corpus covers but it does not. Comparing newest dates alone is not enough: the
  manifest currently ends on the same day as the corpus while missing 568 days in between;
* the analysis corpus is more than `--gap-days` (default 30) behind today
  → fix with `.venv/bin/python scripts/bulk_pipeline.py`;
* a package logged in `data/bulk/_errors.txt` is genuinely absent from the corpus.
  That log is **append-only** — a package that failed once and was ingested on a later
  run stays listed forever — so entries are resolved against the corpus and only real
  gaps are reported.

Run it after any ingest job, and before quoting a coverage date.

## Setup

```bash
cd congressional_record
uv venv .venv && uv pip install -r requirements.txt
# or: python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### API key (required for real runs)

The GovInfo API is rate-limited per key. The shared `DEMO_KEY` allows only ~50 requests/day —
fine for a quick poke, but **far too low** for even a single month. Get a free key in seconds:

1. Sign up at <https://api.data.gov/signup/> (the key works for the GovInfo API).
2. Copy `.env.example` to `.env` and set `GOVINFO_API_KEY=<your key>`.

A real key allows ~1,000 requests/hour. Use `--min-interval` to stay under that ceiling.

## Usage

```bash
# Validate on a single month
.venv/bin/python fetch_crec.py --sample-month 2024-01

# Full backfill of the digital collection (long-running; resumable).
# Omit --end so it always runs through today rather than a stale hardcoded date.
.venv/bin/python fetch_crec.py --start 1994-01-01 --min-interval 0.5

# Just the House + Extensions for one year
.venv/bin/python fetch_crec.py --start 2023-01-01 --end 2023-12-31 --classes HOUSE EXTENSIONS

# Quick smoke test: stop after 5 new granules
.venv/bin/python fetch_crec.py --sample-month 2024-01 --classes HOUSE --limit 5
```

### Options

| Flag | Description |
|---|---|
| `--start` / `--end` | Date range `YYYY-MM-DD` (end defaults to today). |
| `--sample-month YYYY-MM` | Convenience: one calendar month (overrides start/end). |
| `--classes` | granuleClass values to keep (default: all). |
| `--out-dir` | Output directory (default: `./data`). |
| `--api-key` | Override `GOVINFO_API_KEY`. |
| `--min-interval` | Min seconds between API calls (throttle for the ~1,000/hr limit). |
| `--limit` | Stop after N newly downloaded granules (testing). |
| `--overwrite` | Re-download granules already on disk. |
| `-v` | Debug logging. |

## Resumability

Progress is recorded in `data/manifest.jsonl` and on disk. Re-running the **same command**
skips granules already downloaded, so a large backfill can be stopped (Ctrl-C) and resumed
safely. If the run is persistently rate-limited it aborts cleanly with guidance rather than
hanging.

## Notes & caveats

- A full 1994→present backfill is large (tens of GB, hundreds of thousands of granules).
- `data/` and `.env` are git-ignored; no credentials are committed.
- `granuleClass` is GovInfo's own classification; a granule's `title` ("House of
  Representatives") can differ from its class (e.g. a House proceeding may appear in the
  `SENATE`-numbered page sequence). Filter on `granuleClass` for the chamber.
