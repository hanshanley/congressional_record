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

The central registry is `analysis/score/registry.py`. It defines each raw count, scale, construct
family, polarity, codebook version (`2026-10-v5`), and plot eligibility. Every rate uses words as
the denominator.

- **Formal courtesy**: conventional parliamentary address and deference.
- **Profanity**: curated exact curse and obscene forms in a mild and a strong tier. Ambiguous
  neutral vocabulary is excluded.
- **Slurs**: curated exact US ethnic, sexual-orientation and gender-identity, and disability
  slurs. Ambiguous forms are kept in an audit-only list
  and never scored. Occurrence does not imply endorsement; quotation and condemnation still need
  review.

Profanity candidates are cross-checked against the
[LDNOOBW English list](https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words),
the [Surge AI profanity dataset](https://github.com/mod-tc/profanity), and the Kaggle
[Profanities in English collection](https://www.kaggle.com/datasets/konradb/profanities-in-english-collection),
then manually narrowed for congressional speech. None is imported wholesale because each includes
terms that are neutral, topical, identity-related, or too context-dependent for reliable member
attribution. From the Kaggle list, only single-word compounds of roots already counted
(`fuckface`, `dumbshit`, …) were added; multi-word phrases built on an already-counted word were
not, because they would change how existing matches are counted.

Ethnic slurs start from Wikipedia's [List of ethnic slurs](https://en.wikipedia.org/wiki/List_of_ethnic_slurs).
A row is included when its location is the United States, North America, worldwide, or
international (or names African American or Native American targets), or when the location is
blank and the targets or notes tie the term to American usage. Each included row was then sorted
into scored forms, audit-only forms, or unmatched, with the reason recorded. The Kaggle list
added US spellings missing from Wikipedia.

Sexual-orientation and gender-identity slurs start from Wikipedia's
[homophobic](https://en.wikipedia.org/wiki/Category:Homophobic_slurs) and
[LGBTQ-related](https://en.wikipedia.org/wiki/Category:LGBTQ-related_slurs) slur categories.
Disability slurs start from Wikipedia's
[list of disability-related terms with negative connotations](https://en.wikipedia.org/wiki/List_of_disability-related_terms_with_negative_connotations),
restricted to terms used as slurs for disabled people; most of that list is descriptive
vocabulary (`blind`, `handicapped`) or former clinical labels now used as general insults
(`idiot`, `moron`), which are not counted. Terms whose main meaning is something else (bare
`dyke`, `queer`, `fairy`, `Gaylord`, `cripple`, `midget`, `retarded`, which was the statutory term
until 2010) are audit-only. `scorers.py` drops slur matches inside verb uses of `retard`,
archaic `faggot(s) of` bundles and 19th-century `faggot votes`, `fag end`, Latin `homo`
phrases, and line-break hyphenation fragments. Gendered insults (`bitch`, `whore`) remain
profanity, and no form may be both. `analysis/score/lexicons/slurs_provenance.tsv` records every
source row and decision.

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

The finalized 784-passage validation of the July 2026 v3 codebook achieved precision of 89.4% or
better for every category it published. That sample
measured the earlier, broader codebook and did not cover slurs. Codebook v5 (October 2026)
narrows the project to formal courtesy, profanity, and slurs and expands both exact-match
lists, so its precision and recall must be re-estimated on a new blinded sample.
`precision_recall.csv` reports the completed v3 validation rather than presenting it as
validation of later revisions.

Member term summaries retain exact matched surface forms in the underlying daily audit table.
For presentation, explicit inflectional, plural, spacing, spelling, and phrasal variants are
grouped into families before selecting a member's most-used term or the top member for a term.
For example, `damn` and `god damn` contribute to one `damn` family, `fuck` and `fuck you`
contribute to one `fuck` family, and `ass` and `kick ass` contribute to one `ass` family.
The explicit family map is defined in `analysis/speakers.py`; unrelated lexical compounds such
as `motherfucker` and `bullshit` are not stemmed together automatically. Every codebook form,
including intentional singletons, must appear in that map so additions cannot create unreviewed
display families.

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
