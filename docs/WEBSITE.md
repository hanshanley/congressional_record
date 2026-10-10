# The website

The homepage, published at <https://www.themarginoferror.com/congressional_profanity/>, leads
with interactive long-run panels for formal courtesy, profanity, and slurs. Each panel compares Democrats with Republicans from 1873 to the
present and includes visible party controls, direct end labels, hover/focus values, and a
screen-reader data table.

Recent-Congress detail then presents profanity and slurs as separate indicators. Monthly Democratic/Republican trends
are shown within a selected Congress, while the all-Congresses view uses yearly periods. Member
comparisons apply a minimum-word threshold and omit zero-rate members entirely. A measure is shown
only when every row in the selected scope has been scored for it: rows written before a measure
existed load as missing rather than zero, so a new measure stays hidden until the backfill and
historical rebuild workflows have rescored the data. Both pages use
The Margin of Error masthead, footer, and typography (the house tokens in `build_site.py` mirror
`margin_of_error/src/styles/global.css`), and the charts are responsive inline SVGs in that house
style. Hovering or keyboard-focusing a point or bar reveals its raw hits and word denominator, and
hidden data tables expose every value to screen readers. Profanity terms are censored everywhere
they appear in visible text (tables, side panel, tooltips, and the state map); a censored term in
a table reveals itself on hover or focus. Generated PNGs, rendered with `theme.house_style()`,
provide a no-JavaScript fallback for the initial view.

`site/activity.html` holds the secondary exact-value tables:

1. attributed spoken words;
2. sponsored House and Senate bills;
3. sponsored bills that passed at least one chamber;
4. sponsored bills that became law; and
5. nonzero profanity rates per 100,000 attributed words.

It deliberately does **not** collapse these into an “effectiveness” score. Sponsoring a bill that
passes or becomes law is a descriptive milestone, not proof that one member personally caused the
outcome. The legislative rankings currently cover `H.R.` and `S.` measures only; cosponsorships,
resolutions, and amendments are excluded.

## Seed and update bill data

Bill records use two official sources with one canonical schema:

* **Congresses 103-107:** one-time Congress.gov API seed. Set `CONGRESS_API_KEY` in `.env`
  (an existing `GOVINFO_API_KEY` is accepted as a fallback because both use api.data.gov).
* **Congress 108-present:** no-key GovInfo Bill Status bulk XML. Full backfills use the official
  per-Congress H.R./S. archives; scheduled updates fetch only new or changed current-Congress
  records.

```bash
# One-time historical seed; resumable through its checkpoint file.
python scripts/seed_legacy_bills.py

# One-time official bulk backfill, then routine current-Congress refreshes.
python scripts/update_bills.py --backfill 108 --congress 119
python scripts/update_bills.py

# Speech refresh and site render.
python scripts/update_speakers.py
python scripts/update_language.py
python scripts/build_site.py
```

Normalized state is committed as compact Parquet tables:

```text
data/site/speaker_daily/congress_NNN.parquet
data/site/bills/congress_NNN.parquet
data/site/language_daily.parquet
```

The site writes `site/data/congresses.json` and `site/data/congress_NNN.json` for direct reuse.
Congress 103 speech coverage begins on 1994-01-25, so that Congress's speech and profanity
rankings are partial and are labeled accordingly.

## How the site runs unattended

`site/` is a self-contained static dashboard with no application server, database, or external
charting dependency. It is designed to update **unattended**.

### Why it can run unattended

The obvious approach — rebuild everything from the corpus on a schedule — cannot work in CI: the
turn corpus is ~6 GB and is not in the repository. So the site is driven by small **committed
state partitions** instead:

```
data/site/speaker_daily/congress_NNN.parquet    one row per (bioguide, date, chamber)
data/site/bills/congress_NNN.parquet            one row per H.R./S. bill
```

The speaker table is an append-only summary. A scheduled job reads its last date, downloads only
new Congressional Record issues, scores those days, and merges the counts back in. A separate
compact daily aggregate table updates the current-Congress portion of the 1873-present party
series from the same official GovInfo packages. The bill updater independently reads official
Bill Status metadata and replaces only new or changed current-Congress records.

```bash
python scripts/update_speakers.py    # extend the table with newly published days
python scripts/update_language.py    # refresh current-Congress aggregate trend metrics
python scripts/update_bills.py       # refresh current-Congress H.R./S. statuses
python scripts/build_site.py         # render site/ from the committed tables
```

`build_site.py` ranks the **sitting Congress** by default (`--congress latest`); pass a number
for a specific one or `--congress all` for an all-time board. The default is deliberate: it keeps
the scheduled build showing the current Congress and producing the same output as a local run.
The default snapshot timestamp is derived from the newest speech or bill input, so unchanged
inputs produce byte-identical output and no timestamp-only commit. `SOURCE_DATE_EPOCH` and
`--generated-utc` remain available for an explicit reproducible-build timestamp.

`.github/workflows/update-site.yml` checks for new data twice daily at 07:20 and 19:20 UTC and
commits the refreshed tables and site. Each run replaces a seven-day aggregate window and a
three-day member window, so late or corrected GovInfo issues are repaired rather than appended
twice. The Margin of Error deployment checks twice daily after those source refreshes and
publishes the site at
<https://www.themarginoferror.com/congressional_profanity/>. The earlier
`/professional_profanity/` path and the former GitHub Pages endpoint publish only redirects to
the new address.

**No API key or repository secret is required for scheduled updates.** Record issues and current
bill statuses both come from public GovInfo bulk URLs. The Congress.gov API key is used only for
the one-time local seed of Congresses 103-107.

**PDF-only Record issues are ingested too.** When an archive has no HTML transcripts, the bulk
ingester extracts its section PDFs in column order rather than dropping the day. Speaker
segmentation and scoring use the same rules as the HTML path. Because these preliminary issues
can lack member-level MODS, the fallback resolves names against the public
`unitedstates/congress-legislators` current and historical rosters, restricted to the issue date
and chamber (including dated party changes). Ambiguous names remain unattributed; they are not
guessed. PDF-derived turn IDs contain `#pdf-`, coverage manifests list `pdf_packages`, and later
HTML replaces PDF turns rather than double-counting them. Image-only or unrecognized PDFs fail
explicitly instead of publishing empty scores; this fallback does not perform OCR.

HTML and manifest ingestion also use the dated roster when a direct speaker's MODS identity is
missing or incomplete; usable MODS identities are retained and ambiguous matches are not guessed.
Both representations exclude Senate non-spoken statements: HTML `<bullet>` markers are preserved
and interpreted like the PDF's printed bullet symbols. `SADDITIONAL` statements and
`HADDSPONSORS` lists are also excluded by metadata. Printed page labels and clock times do not count as speech,
editorial notes and permission-to-speak annotations are not attributed to members, and HTML and
typographic PDF quotation marks receive the same quotation exclusion. PDF editorial subheadings
do not interrupt the current speaker; inserted letters and other printed blocks remain excluded.
Re-ingesting an issue replaces its stored turns so corrected attribution and eligibility take
effect without duplicating the issue. Previously downloaded plain text that already lost its
`<bullet>` markers must be fetched again with `--overwrite` before rescoring; the scheduled bulk
updaters always download fresh source archives.

To enable it:

1. **Settings → Pages → Source: GitHub Actions.**
2. Seed the table once from the local corpus (CI only ever appends to it):

   ```bash
   python -c "from pathlib import Path; from analysis.speakers import build_daily; \
     build_daily(Path('data/interim/turns'), Path('data/site/speaker_daily'), \
       only_files=sorted(Path('data/interim/turns').glob('govinfo_bulk_*.parquet')))"
   ```

The workflow runs the test suite before it publishes: a scheduled job that silently ships wrong
numbers is worse than one that fails loudly. Tests enforce the schedules and updater wiring,
current-Congress bootstrap behavior, correction lookbacks, full-window replacement, duplicate
turn suppression, eligibility exclusions, and preservation of historical long-run rows.

### Embedding it in a blog

The build writes machine-readable output alongside the HTML, so a blog can pull the numbers
directly rather than screen-scraping:

```
site/index.html                 primary language-analysis page
site/activity.html              secondary member-activity and bill tables
site/data/congresses.json      available Congress selectors
site/data/congress_NNN.json    leaderboards, interactive chart data, definitions, and coverage
site/data/leaderboard.json     compatibility profanity leaderboard for the initial view
site/data/timeseries.json      chamber-level profanity rate per year
site/data/meta.json            build stamp, thresholds, coverage, caveat text
site/data/long_run_language.json compact data for the interactive long-run panels
site/figures/*.png             no-JavaScript/compatibility charts in the project's house style
```

### Attribution safeguards

Naming individuals is a reputational claim, so the counts are deliberately conservative:

* **Quotations are excluded.** The Record marks quoted passages with TeX-style ``` `` … '' ```.
  A member reading someone else's words is not swearing — and this is not a rounding error:
  **5.3%** of raw profanity hits in the 119th Congress fall inside quotations, easily enough to
  reorder a leaderboard. Excluded hits are still counted and shown in their own column, so the
  exclusion is auditable rather than invisible.
* **A stable id is required.** Only turns carrying a Bioguide ID are counted; surnames alone
  collide (`Mr. SMITH`) and drift between Congresses.
* **Procedural speech is excluded.** The Chair and presiding officers dominate the Record by
  volume but are not making personal remarks.
* **Printed material is excluded.** Bills, amendments, exhibits, and other material inserted into
  the Record are retained as procedural source turns rather than attributed as spoken words.
  Long turns duplicated by overlapping GovInfo granules are content-deduplicated.
* **Rate, not raw count**, so prolific speakers are not penalised — with a minimum word threshold
  (default 25,000 attributed words), because one profanity in 300 words of floor time is noise,
  not a ranking. Members below the threshold are omitted rather than shown with an unstable rate.
* The profanity lexicon is a **conservative, hand-curated list**, not an exhaustive word list.
  It includes explicit surface forms and compounds while omitting ambiguous ordinary, anatomical,
  medical, and identity terms (the repo's earlier broad list flagged "strips" and "erected").
* Raw matched forms remain in the audit table, but member “most-used term” and per-term leader
  summaries group explicit inflectional, plural, spacing, spelling, and phrasal variants into
  documented families (for example, `damn`/`god damn`, `fuck`/`fuck you`, and `ass`/`kick ass`).
  Distinct lexical compounds such as `motherfucker` and `bullshit` remain separate. Every
  configured form, including intentional singletons, must be assigned explicitly.
* The term explorer can rank families by total use or show each family's top member, filtered by
  Congress, party, and chamber. Its state tile map applies the same scope and displays each state's
  most-used family; states without an accepted use remain visibly unclassified.

The Congressional Record is a lightly edited transcript, not a verbatim one, and members may
revise their remarks — worth stating plainly wherever these numbers are published.
