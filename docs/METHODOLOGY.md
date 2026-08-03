# Congressional discourse methodology

## Provenance

Every metric is computed by Python from a real speaker turn in either the Stanford Hein
Congressional Record corpus or a GovInfo CREC package. `turn_id`, source, Congress, chamber,
speaker metadata, and exact source text are preserved during ingestion. Generated examples and
synthetic observations are never inserted into metric tables.

The primary long-run series uses Hein through Congress 114 and GovInfo from Congress 115.
`data/processed/coverage/source_metadata.json` records source ranges and drives plot provenance.
The separate `civility_metrics_by_source.parquet` retains both sources in overlap years.

## Components

The central registry is `analysis/score/registry.py`. It defines each raw count, denominator,
scale, construct family, polarity, codebook version, and plot eligibility.

- **Formulaic courtesy**: conventional parliamentary address and deference.
- **Gratitude/praise**: explicit thanks, praise, appreciation, commendation, or respect.
- **Bipartisan cooperation**: explicit cross-aisle work or bipartisan spirit.
- **Personal attack**: high-precision attacks on honesty, integrity, competence, character, or
  fitness.
- **Misconduct allegation**: exact curated language alleging corruption, fraud, bribery,
  obstruction, abuse of power, or similar conduct. Direct negation patterns and selected
  legal-title references are excluded. It is not evidence that misconduct occurred.
- **Ideological label**: tracked separately and not automatically treated as disrespect.
- **Profanity and identity slurs**: curated exact forms. Neutral topical vocabulary is excluded;
  slur occurrence requires quotation and endorsement review.

Most rates use words as the denominator. Cross-party context rates use deduplicated out-party
reference events and report affected contexts per 100 references.

## Cross-party targeting

Target detection is independent of tone matching. The detector uses party nouns, high-precision
party phrases, and cross-aisle idioms resolved against the speaker's party. Generic
`democratic` language is not a Democratic Party reference. Overlapping target spans are merged.

Conditional context rates use the sentence/clause containing each reference, bounded to 300
characters on either side for OCR run-ons. Separate nearby-intensity diagnostics retain a
200-character window and are labelled **near an out-party reference**. Proximity does not prove
that a particular phrase targets the party; validation estimates the precision of that
interpretation.

## Source overlap

After GovInfo 1994-2016 is ingested:

```bash
python -m analysis.run aggregate
python -m analysis.run calibrate
```

Calibration pairs Congress × chamber × party cells across Hein and GovInfo. It reports
correlations, differences, and ratio dispersion. A multiplicative source adjustment is recommended
only when at least 20 paired cells have Spearman correlation of at least 0.70 and the interquartile
range of log source ratios is no wider than `log(1.5)`. Otherwise, sources remain visibly separate.

## Model-assisted validation

```bash
python -m analysis.run sample-validation
```

The sampler writes at least 600 deterministic, stratified, real-text passages when full source
coverage is present. Production scores and sampling strata are stored separately from blinded
passages. Two independent model passes use `docs/VALIDATION_RUBRIC.md`; a separate adjudication
pass resolves disagreements. This is disclosed model-assisted face-validity and consistency
checking, not independent human ground truth.

The finalized 784-passage validation achieved overall precision of 89.4% or better for every
published category and at least 80% precision in every era/source stratum with 10 or more coded
examples. The codebooks intentionally favor precision over recall; category-level recall ranges
from 16.5% for personal attacks to 94.8% for profanity. `precision_recall.csv` reports every
category and stratum rather than hiding low-recall results.

## Remaining interpretation limits

OCR error, quotation, sarcasm, historical language drift, incomplete party attribution, and source
differences can affect rates. Charts are descriptive and do not identify a causal effect of
polarization or any other political process.

## Member activity dashboard

The static site joins floor-speech summaries to official legislative records by Bioguide ID.
It publishes separate rankings rather than a composite score:

- attributed non-procedural House/Senate spoken words, with turns and active days as context;
- distinct sponsored House and Senate bills (`H.R.` and `S.` only);
- sponsored bills with an official measure-level House or Senate passage action;
- sponsored bills assigned a public or private law number; and
- unquoted curated profanity hits per 100,000 attributed words, subject to the site's minimum-word
  threshold.

Bill sponsorship, passage, and enactment are descriptive milestones. Sponsorship is not sole
authorship, and a bill passing or becoming law does not establish that its sponsor personally
caused that outcome. Cosponsorships, resolutions, amendments, committee productivity, and vote
behavior are not included in the first version.

Canonical bill rows preserve their official source URL, update timestamp, sponsor Bioguide ID,
matched passage action codes, and law citation. GovInfo Bill Status bulk XML supplies Congress 108
forward without an API key. Congress.gov API data supplies Congresses 103-107 through a one-time,
keyed seed. Both adapters emit the same schema and are checked against overlapping Congress 108
fixtures. Routine automation refreshes only the current Congress; historical corrections require
an explicit full refresh.

The committed speech table begins Congress 103 on 1994-01-25 rather than at the start of the
Congress, so Congress 103 speaking and profanity rankings are explicitly labeled partial.
Extensions of Remarks remain in the underlying audit table but are excluded from these member
rankings because they are submitted for publication rather than spoken on the floor.
