# Congressional Record Transcripts

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

# Full backfill of the digital collection (long-running; resumable)
.venv/bin/python fetch_crec.py --start 1994-01-01 --end 2026-06-25 --min-interval 0.5

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

## Analysis: measuring the decline of comity between the parties

Beyond downloading, this repo includes a reusable **analysis pipeline** (`analysis/`) that
scores every speaker turn for markers of cross-party civility vs. hostility and produces
time-series charts. It unifies two corpora into one speaker-turn table:

* **Stanford *hein* corpus** (1873–2017, congresses 043–114) — already speaker-segmented with
  party labels. Download the `hein-bound.zip` / `hein-daily.zip` from
  <https://data.stanford.edu/congress_text> into `data/raw/` (extract not required; the
  ingester reads the zips directly). On macOS these are zip64 >4 GB — use `ditto`/Python
  `zipfile`, not `unzip`, if you do extract them.
* **GovInfo CREC** (2017→present) — the `fetch_crec.py` output above; granules are segmented
  into speaker turns and party is attributed from MODS. For a **much faster** bulk load that
  bypasses the API's rate limit, use `ingest-govinfo-bulk` (below), which downloads whole-day
  package zips directly from `www.govinfo.gov/content/pkg` (no API key, not rate-limited),
  parses each day's MODS per granule, and deletes each zip after ingest.

### What it measures

* **Comity/deference** phrases ("my distinguished colleague", "the gentleman from", "reach
  across the aisle", "I yield to")
* **Hostility/attack** language and **profanity** (a comprehensive, tiered lexicon: mild/strong/slurs)
* **Cross-party reference tone** — out-group references resolved to the speaker's party, plus
  **directed** hostility/comity in the text window around each reference
* The **"Democrat party"** pejorative marker; optional **VADER sentiment** (see *Toxicity* below)

All lexical rates are per 1,000 words, grouped by `(congress, chamber, party)`, so overall
trends split by party, by chamber, and directed D↔R asymmetry can all be plotted.

**Fuzzy keyword matching.** Lexicon terms match their morphological variants by default
(`Scorers(fuzzy=True)`): single words expand to plurals/verb-forms via suffix rules plus an
irregular-plural table ("colleague"→"colleagues", "coward"→"cowards", "gentleman"→"gentlemen"),
and multi-word phrases inflect **every** content word inline in the regex, so "reach across the
aisle" also matches "reaches/reached/reaching across the aisle". Short tokens (< 4 chars) are
matched literally so obfuscation stubs are never expanded into ordinary words, and profanity
surface-forms are de-duplicated across tiers so nothing is double-counted. Pass `fuzzy=False`
for strict exact matching.

**By party and by chamber.** `viz` renders both the party split (D vs R vs other) and a
**chamber × party** split (House vs Senate, each by party): `overview_by_chamber.png`, a
`*_by_chamber.png` per metric, and an extra CSV `metrics_by_congress_chamber_party.csv`.

**Toxicity methodology.** The headline signals are transparent, auditable **lexical rates**
(hostility/profanity per 1k words) — not a black-box classifier. Optional VADER sentiment
(`--sentiment`) is scored **per sentence and averaged** (VADER's `compound` saturates on long
passages, so scoring a whole speech is biased), exposing `mean_sentiment` and `mean_neg_share`,
which are **sentence-count weighted** in the aggregate to match the word-weighting of the other
metrics. The interactive notebook cross-checks that these independent negativity signals agree
(and can optionally validate a sample against the Detoxify neural model).

### Explore interactively

```bash
.venv/bin/python -m pip install jupyter   # or: uv pip install jupyter
jupyter lab notebooks/congressional_civility.ipynb
```

The notebook loads the metrics table, plots the party and chamber×party trends, and runs the
toxicity cross-validation against real data.

### Run it

```bash
uv pip install -r requirements-analysis.txt        # pandas, pyarrow, matplotlib, vader, ...

python -m analysis.run ingest-hein                 # hein zips -> data/interim/turns/*.parquet
python -m analysis.run ingest-govinfo-bulk         # 2017-present via day-zips (fast, no rate limit)
python -m analysis.run aggregate                   # score all turns (fuzzy) -> data/processed/metrics/
python -m analysis.run viz                         # charts -> data/reports/figures/

# or the whole hein pipeline in one go (add --sentiment for VADER):
python -m analysis.run all
```

Outputs: `data/processed/metrics/civility_metrics.{parquet,csv}`,
`metrics_by_congress_chamber_party.csv`, and PNG charts under `data/reports/figures/`
(an `overview.png` small-multiples, `overview_by_chamber.png`, and one `*.png` +
`*_by_chamber.png` per metric). All `data/` outputs are git-ignored.

Charts use a shared **Substack-style** plotting toolkit (`analysis/plotting/`, matched to the
`uk_decline` portfolio look) — see [`docs/PLOTTING.md`](docs/PLOTTING.md) for the palette,
helpers, and how to reuse it.

## Source & attribution

Congressional Record data courtesy of the U.S. Government Publishing Office via GovInfo
(<https://www.govinfo.gov/>). See the GovInfo API docs at <https://api.govinfo.gov/docs/>.
The parsed hein corpus is from Gentzkow, Shapiro & Taddy, *Congressional Record for the
43rd–114th Congresses* (Stanford Libraries, 2018), ODC-BY 1.0.

## Tests

Offline unit tests (no network) cover MODS parsing, the downloader hardening, and the
analysis scorers/ingesters:

```bash
.venv/bin/python -m pytest tests/ -q   # if pytest installed
# or run each file directly:
.venv/bin/python tests/test_parsing.py
.venv/bin/python tests/test_hardening.py
.venv/bin/python tests/test_analysis.py
```
