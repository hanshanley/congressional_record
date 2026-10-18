# The website

The `terms/` page presents individual annual graphs for the five most-used swear-word families
in the GovInfo member data (1994–present), with CSV/JSON downloads. It combines forms such as
`fuck` and `fucking`, keeps distinct compounds separate, and ranks families by total D/R matches.
The figures are lexical counts, not verified instances of personally uttered curses; quoted,
inserted or literal uses can survive imperfect filtering. These graphs are not the Stanford
series back to 1873. The page uses the same responsive inline SVG renderer as the long-run
graphs: transparent backgrounds, house typography, party toggles, hover/focus values,
and screen-reader data tables. Publication PNG downloads remain available and provide
a no-JavaScript fallback. The daily build updates the page and its matching publication PNGs.

Both the dashboard and the family page expose the full included-word codebook, not just
families with observed matches. The word-family selector initially shows `fuck` and its
inflections, with the accepted count for the current scope. The family page can graph any
included family, even when every observed year has zero matches; its full-codebook CSV and
JSON retain the exact counts and word denominators. The five publication PNGs still show
the most-used families. Missing term data are shown as unavailable, never as zero.

The October 17, 2026 audit of member data through October 15 found no unquoted f-word
matches surviving the attribution safeguards.
The 26 previously attributed base-family hits on February 9–11, 2021 were in impeachment
video/inserted material, not identified members' own remarks. Quoted uses, including the
July 23, 2020 floor quotation, are excluded by policy; masked source spellings are not
expanded into guessed words. This describes this dataset and filtering, not an assertion
that no member ever said the word.

### Source-level profanity audit

`Audit all profanity families` is a read-only workflow that independently scans original
GovInfo transcript words across all 17 covered Congresses and compares the resulting
member-day surface-form counts with the stored table. Its reference matcher walks words
and whitespace rather than using the production phrase regex. `Rebuild historical language`
also accepts `audit_only: true` to audit the Stanford/GovInfo aggregate corpus without
replacing published data.

Audit artifacts retain every lexical occurrence with its original matched text, source turn
identity, offsets, surrounding context, family, and accepted/excluded disposition.
Quotations and inserted material remain available for review rather than disappearing.
The scorer and codebook hashes identify the audited rules. These are lexical consistency
checks and evidence for contextual review, not an automatic semantic certification that
every matched word is an intentionally uttered curse. Website censorship is display-only
and is never the reference text for counting.

The context review also checks transcription homographs and speech segmentation. Narrow,
verified literal contexts are excluded, and indented quotations no longer discard the
member's own resumed floor paragraphs. A missing surname period is recognized only when
followed by an explicit floor address; identities are still resolved from source metadata
or the dated roster, never guessed. Short recovered fragments retain their original-segment
identity for overlap deduplication.
Long duplicate remarks in overlapping granules are also compared independently of
surrounding bill metadata and editorial underscore separators. Original MODS page ranges
must overlap; identical remarks on disjoint physical pages or repeated within the same
granule are not discarded. Broader page views are processed before their individual views.
Malformed source page bounds are logged and are never sorted into an invented interval;
that granule cannot use page-dependent body matching. The original exact-segment identity
remains the fallback when physical-page metadata is unavailable.

Bill-introduction headings cannot restart the preceding member's remarks. Verified
external radio and telephone transcripts, telegram text, and directly attributed
external remarks lacking quotation marks use source-specific
opening and closing cues, not a blanket rule based on a word or topic; nested quotations
are counted only once. These reviewed regions affect member attribution, not the
historical aggregate's existing treatment of quoted language.
Reviewed quotations also distinguish external words from a member's own sarcastic
bill names. Source-specific delimiter repairs preserve offsets when mixed quotation
punctuation would otherwise swallow unquoted floor remarks. The original text is unchanged.
An explicitly introduced, indented quotation can be a single line; it is still separate
from the member's resumed remarks.

Audit artifacts also retain `candidate_turns.parquet`: the complete original normalized
turn for each lexical candidate, including excluded cases, so contextual review is not
limited to an excerpt. HTML, PDF, member-scoring, and codebook hashes identify the rules.
API omissions of known stored dates are checked against the official public day archive;
an unresolved missing date still fails coverage rather than being treated as zero.

Run the source audit with `save_candidates: true` to retain independently checked
`speaker-congress-N` partitions as artifacts. This does not modify published data.
These artifacts now also retain every normalized original turn under `originals/`,
including zero-match turns, so source-normalization corrections can be replayed with
complete word denominators rather than another corpus download.
After reviewing all scopes and matching the audited rule hashes, the existing backfill
workflow's `source_run_id` recovery input can publish those saved partitions without
downloading the entire corpus again. A normal historical rebuild now audits the original
corpus before rebuilding metrics and retains publication inputs for recovery.
Replacement partitions must reconcile to every original-source accepted form before
being saved. Recovery artifacts also retain the full historical metrics and coverage
tables, not only the rendered figures. Rebuild the long-run JSON from those checked
metrics during recovery: the committed JSON is not refreshed until the publication step.
For Congresses 115 onward, candidate runs also emit `aggregate-congress-N` metrics.
These are separately audited with the aggregate quotation policy and reconciled by
party, chamber, and profanity tier, using the same downloaded original turns. They are
not reconstructed from member counts and do not require another GovInfo corpus download.

`Audit retained historical source batches` preserves all 72 normalized Stanford
partitions in six checksum-verified artifacts before scoring. Independent batch jobs
then produce complete by-source metrics and original-word audits, without downloading
the modern GovInfo corpus or publishing provisional counts. Its `source_run_id` input
reuses those complete originals for subsequent scoring-policy corrections.

Subsequent profanity-only context corrections can use
`scripts/rescore_audited_profanity.py` with the complete retained original turns and an
immutable checkout of the audited producer. It runs both actual scoring versions, requires
every baseline profanity counter and surface form to reconcile, and refuses changes to the
codebook, tokenization, eligibility, or courtesy. Only then
does it replace profanity numerators and recompute their rates using the original full-corpus
word denominators. Its audit is explicitly a replay of the retained candidate turns, not a
claim to have downloaded or scanned every zero-match turn again.
Historical normalization and party rules must also remain byte-identical. Aggregate replay
does not apply member-only quotation or attribution rules; member replay still validates
those rules separately.

The replay also permits additive source-reviewed quotation boundaries without changing
any member-scoring logic. Each new opening cue must contain a complete profanity occurrence,
which proves that every affected original turn was retained. Only the exact additional
quoted words leave the member denominator; slurs can move into quoted counters but cannot
be created or lost. Aggregate quotation policy and denominators are unchanged.
Site publication rejects a retained historical audit when its producer hashes no longer
match the current policy, including nonempty reviewed-context data. A long-running rebuild
cannot silently publish pre-correction counts after rebasing newer source rules.

Reviewed OCR or literal-sense exclusions can be content-addressed to a complete normalized
turn in `analysis/score/lexicons/profanity_contexts.json`. Exact text hashes, original offsets,
matched words, codebook tiers, and source identities prevent a decision about one corrupt
word from suppressing the same actual curse elsewhere. These exclusions never alter source
text or its word denominator, and their data are part of the aggregate cache fingerprint.
An ambiguous context is not automatically classified as a confirmed error. Original-turn
validation is required before promoting a provisional review decision into this codebook.
`scripts/compile_profanity_context_reviews.py` joins each semantic decision to the current
audit by original identity, exact offsets, and form, then verifies the complete original turn
and lexical occurrence before producing a content-addressed manifest. Missing, duplicate,
or conflicting decisions fail compilation; uncertainty never becomes an exclusion.

The complete historical review covers all 10,113 original codebook occurrences in
17,766,619 normalized turns across Congresses 43–114. All 8,420 candidate turns were
retained in full. Reviewers read each occurrence, using adjacent original debate turns
and archival pages where a short or corrupted turn lacked context. The 3,192 verified
nonprofane occurrences are content-addressed exclusions; some were already outside
the counted scope, so this is not a claim that 3,192 published hits were removed.
Genuine quoted, religious, and metaphorical uses remain in the aggregate series.
The final manifest was checked across all batches for identical-text conflicts.

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
