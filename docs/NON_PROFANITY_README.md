# README: how the non-profanity categories actually work

> **Historical document.** Codebook v5 removed gratitude/praise, cooperation, personal attack,
> misconduct, combined comity, ideological labels, out-party context, the “Democrat party”
> pejorative, and VADER sentiment. The project now measures formal courtesy, profanity, and
> slurs only. This critique describes the codebook as of commit `94a31af`; the lexicon
> files it links to can be read at that commit.

**Bottom line:** these are reproducible counts of selected words and phrases, not
measurements of a member's true civility, sincerity, cooperation, or wrongdoing.
Some labels describe the counts reasonably well; others suggest more contextual
understanding than the code supplies. Use them as lexical diagnostics, not a
validated ranking of which party behaves better.

This document explains the implementation, demonstrates failure modes, critiques
the evidence, and proposes a replacement. **The replacement is a proposal, not
implemented scoring behavior.** Profanity scoring is outside this document's scope;
identity slurs are included because the implementation treats them separately.

## 1. Where the numbers come from

| Source file | Responsibility |
|---|---|
| [`scorers.py`](../analysis/score/scorers.py) | Matching, exclusions, reference detection, and per-turn counts. |
| [`lexicons/`](../analysis/score/lexicons/) | The active lists of counted expressions. |
| [`registry.py`](../analysis/score/registry.py) | Metric names, raw-count keys, denominators, units, and codebook version. |
| [`aggregate.py`](../analysis/aggregate.py) | Long-run totals by source, Congress, chamber, and party; primary-source selection. |
| [`daily_language.py`](../analysis/daily_language.py) | Daily D/R House/Senate totals for the six headline categories. |
| [`validate.py`](../analysis/validate.py) | Sampling and comparison with adjudicated model annotations. |

The default scorer lowercases each turn and tokenizes with
`[a-z0-9]+(?:[-'\u2019][a-z0-9]+)*`. Internal apostrophes and hyphens are preserved;
numbers count as tokens too. The rate denominator is this scorer's token count,
not the input table's stored `word_count`.

Single-word entries are counted by token membership. Multi-word entries use
word-boundary regular expressions with whitespace between words. This does not
recognize paraphrases, semantic similarity, or arbitrary intervening words.

By default, words of at least four characters are expanded with simple plural and
verb-suffix rules, plus a small irregular-form table. Multi-word phrases expand
each eligible content word. This is **rule-based inflection, not linguistic
lemmatization**: it can generate implausible forms and miss real ones. Misconduct
and identity-slur lists use exact curated forms instead. `fuzzy=False` disables
expansion for the other lists.

Within one lexicon, phrase matching prefers longer alternatives and suppresses
single-word hits inside matched phrases. Counts are not globally deduplicated
across different categories. Repeated occurrences count repeatedly.

Only the named active lists are loaded. Files ending in `_ambiguous_audit.txt`
and broad legacy codebooks are not extra production detectors.

### Rates and population

For a group of included turns:

```text
category_per_1k = 1,000 * sum(category_hits) / sum(scorer_tokens)
context_per_100_refs = 100 * sum(affected_references) / sum(outgroup_refs)
```

The pipeline sums counts and denominators first; it does not average speech-level
rates. A 10,000-token speech contributes ten times the word exposure of a
1,000-token speech. That measures language volume, not the typical member.

Long-run aggregation excludes turns marked procedural by default, retains groups
outside D/R and House/Senate, and selects Hein through Congress 114 and GovInfo
from Congress 115 for the primary series. Headline floor comparisons restrict the
relevant parties/chambers. Daily aggregation explicitly requires D/R, House/Senate,
a date, and a nonprocedural turn.

**Quotation caveat:** `score_turn`, long-run aggregation, and daily language
aggregation do not mask embedded quotations. Separate member profanity summaries
in [`speakers.py`](../analysis/speakers.py) mask quotations before scoring. Those
attribution safeguards must not be assumed to apply to all language metrics.
An ingested nonprocedural turn can still contain quotations or read-in language.

The long-run finalizer returns zero for a context rate with no references, and
uses a denominator of one for a zero-word group. These are implementation
conventions, not evidence of zero hostility; unavailable exposure should ideally
be shown as missing.

## 2. What each category counts

The linked codebooks are authoritative. Examples below illustrate their scope,
not exhaustive linguistic definitions.

| Category / output | Actual detector | What the count does **not** establish |
|---|---|---|
| Formulaic courtesy: `formal_courtesy_per_1k` | [`formal_courtesy.txt`](../analysis/score/lexicons/formal_courtesy.txt): parliamentary address such as `my distinguished colleague`, `the gentleman from`, `i yield to`, `with all due respect`, plus standalone `comity` and `civility`. | Sincerity, warmth, cross-party respect, or even an act of courtesy. Mentioning civility also counts. |
| Gratitude/praise: `gratitude_praise_per_1k` | [`gratitude_praise.txt`](../analysis/score/lexicons/gratitude_praise.txt): thirteen expressions, including `i thank my colleague`, `i salute`, `i congratulate`, and `i have the highest respect`. | Who is praised, whether it is sincere, or whether it concerns Congress. Many ordinary expressions of thanks are absent. |
| Cooperation: `cooperation_per_1k` | [`cooperation.txt`](../analysis/score/lexicons/cooperation.txt): six entries involving reaching/working across the aisle, `in a bipartisan`, or `bipartisan spirit`. | Successful joint work. Calls for cooperation, failed cooperation, and some institutional descriptions can match. |
| Personal attack: `hostility_per_1k` | [`hostility.txt`](../analysis/score/lexicons/hostility.txt): eleven entries including `liar`, `hypocrite`, `coward`, `phony`, `incompetent`, `buffoon`, and `shame on you`. | A resolved personal target, speaker endorsement, or exhaustive coverage of attacks. No target is required for the headline count. |
| Misconduct language: `misconduct_per_1k` | [`misconduct.txt`](../analysis/score/lexicons/misconduct.txt): fifteen exact entries, including `corrupt`, `corruption`, `bribery`, `money laundering`, `obstruction of justice`, and `abuse of power`. | That an allegation was actually asserted against someone, that it targets the other party, or that wrongdoing occurred. Legal-topic discussion can match. |
| Combined comity: `comity_per_1k` | Sum of courtesy, gratitude/praise, and cooperation hits. | A separately trained or validated civility construct. It combines unlike behaviors with equal per-hit weight. |
| Ideological labels: `ideological_label_per_1k` | [`ideological_labels.txt`](../analysis/score/lexicons/ideological_labels.txt): `socialist`, `communist`, `marxist`, `fascist`, `authoritarian`, `the radical left`, and `the radical right`, with default inflection. | An insult or personal attack. This family is registered as neutral. |
| Identity slurs: `profanity_slurs_per_1k` | [`slurs.txt`](../analysis/score/lexicons/slurs.txt): a separate curated exact-form list. The historical output name retains `profanity_`, but these hits are excluded from total profanity. | Endorsement or identity-directed abuse by the speaker. Quoting or condemning a slur still requires contextual review. |
| Party-label marker: `democrat_party_pej_per_1k` | Exact case-normalized regex `\bdemocrat\s+party\b`, wherever it occurs. | Pejorative intent. Despite its name and `target_required` registry flag, this detector does not enforce an opposing-party target or speaker-party restriction. |

**Optional sentiment is different.** With `use_sentiment=True` (the analysis CLI's
`--sentiment` option), VADER scores fragments split at whitespace following
`.`, `!`, or `?`. The scorer averages sentence compound scores and negative
shares within each turn; long-run aggregation weights those averages by the
number of sentence fragments, exposing `mean_sentiment` and `mean_neg_share`.
These are not per-word lexical rates, are not among the headline categories,
and do not resolve targets or distinguish policy pessimism from personal abuse.
The lightweight splitter can also mistake abbreviations for sentence boundaries.

### Existing contextual exceptions

Cooperation excludes `in a bipartisan board/commission/committee`, but this is a
narrow pattern, not general recognition of substantive cooperation.

Attack exclusions include `phony price/expense`, `mental/legal incompetent(s)`,
selected `not ... coward/liar/incompetent` expressions, and a narrow question
pattern ending in `a liar?`. They do not provide general negation or quotation
handling. The fast counter subtracts the number of exclusion matches and clamps
at zero; the validation span path instead removes overlapping spans.

Misconduct uses span-based exclusions for the Foreign Corrupt Practices Act and
selected negated corruption expressions. It also checks up to 80 preceding
characters for particular negation constructions and carries negation across
simple `and`/`or` coordination. This improves some cases but does not parse who
accuses whom, denial scope generally, reported allegations, or ordinary legal
discussion.

## 3. How "other party" and "directed" metrics work

Reference detection combines the following, merging overlapping spans:

| Recorded speaker party | Party-name detector |
|---|---|
| D | Tokens `republicans` or `gop`; `republican` followed by party, colleague(s), member(s), caucus, leadership, side, or conference. |
| R | Tokens `democrat` or `democrats`; `democratic` followed by the same selected nouns. |
| I, other, or unknown | No party-name detector. |

All parties can additionally match [`outgroup.txt`](../analysis/score/lexicons/outgroup.txt)
idioms such as `across the aisle`, `the other side`, or `the other party`.
Consequently, an independent can get idiom hits without a resolved opposing party,
and a generic "other side" need not refer to congressional partisans at all.
The detector does not resolve named individuals, pronouns, or historical party
references. Bare singular `Republican` is not equivalent to plural `Republicans`.

Three different quantities must be distinguished:

* **Reference frequency:** `outgroup_ref_per_1k` counts reference spans per
  1,000 total tokens, regardless of tone.
* **Nearby hit intensity:** `directed_comity_per_1k`,
  `directed_hostility_per_1k`, and `directed_misconduct_per_1k` count lexical hits
  in merged windows extending 200 characters before/after reference spans, divided
  by **all included tokens**, not only window tokens. Overlapping windows merge.
* **Affected reference share:** `outgroup_{comity,hostility,misconduct}_contexts_per_100_refs`
  checks the context of each reference, stopping at `. ! ? ;` or newline and
  bounded to 300 characters on either side. Each reference contributes at most one
  affected event per category. Multiple references in one clause can each be
  affected by the same expression.

**None establishes direction.** Even a shared clause can praise one person and
attack another. Characters are not tokens; punctuation and OCR affect the context
boundaries. The registry's public titles mostly acknowledge proximity, but the
internal `directed_` names should not be interpreted as target resolution.

## 4. Reproducible reasons to be skeptical

These are **constructed diagnostic sentences**, checked against the current
scorer. They are not Congressional Record quotations, corpus observations, or
estimates of error frequency. Unless noted, the speaker party is D.

| Diagnostic input | Current raw output | Interpretation problem |
|---|---|---|
| `We should restore civility.` | Courtesy 1. | Mention of a desired norm is coded as courtesy. |
| `With all due respect, Republicans are wrong.` | Courtesy 1; references 1. | Formal address is not evidence of substantive warmth. |
| `I congratulate the local school on its anniversary.` | Gratitude/praise 1. | Praise is not necessarily congressional or cross-party comity. |
| `We failed to work across the aisle.` | Cooperation 1; references 1. | Explicit failure still matches cooperation language. |
| `The hearing examined money laundering.` | Misconduct 1. | Topic mention is not an allegation against anyone. |
| `Republicans condemned corruption.` | Misconduct 1; affected misconduct references 1. | Condemning misconduct is not accusing Republicans of it. |
| `Republicans are here. The foreign dictator is a buffoon.` | Attack 1; nearby attack 1; affected attack references 0. | The 200-character window crosses a sentence and picks up a different target. The clause diagnostic avoids this case, but does not solve targeting generally. |
| `Republicans and Democrats work together.` with party I | Cooperation 0; references 0. | Plain joint-work language is missing from the lexicon; independent party-name references are not resolved. |
| `The Democrat party convention begins today.` | Party-label marker 1. | Occurrence alone does not identify derogatory intent. |

To inspect any example without downloading data or installing analysis packages:

```bash
python3 - <<'PY'
from analysis.score.scorers import Scorers
print(Scorers().score_turn("Republicans condemned corruption.", "D"))
PY
```

## 5. Critique: what the validation does and does not justify

The existing approach has real strengths: transparent codebooks, deterministic
matching, separate construct families, raw counts, and explicit source provenance.
Those make the numbers inspectable. **Reproducibility is not construct validity.**
The main weaknesses are:

1. **Labels outrun measurement.** Courtesy is mostly ritual; cooperation is mostly
   vocabulary; misconduct includes topics; "directed" is proximity. These cannot
   jointly establish a party's underlying civility.
2. **Missing language can bias comparisons.** Conservative lists may capture a
   different fraction of attacks or praise in different eras, chambers, or parties.
   Equal code does not imply equal detection error.
3. **Reference and outcome partly overlap by construction.** `Across the aisle`
   helps detect both cooperation and out-group references. Their association is
   partly built into the dictionaries, not wholly independent evidence.
4. **The validation target differs from the published quantity.** The reporter
   tests whether a passage has any category hit (`count > 0`), not whether each
   hit is correct or counts per 1,000 words are unbiased. It does not separately
   validate the three directed families or their denominators.
5. **Model agreement is not independent human ground truth.** Two model passes
   and adjudication can share interpretive errors. The final precision/recall
   reporter does not require a target or filter quoted/read-in cases; it excludes
   uncertain labels and reports no uncertainty intervals.
6. **Sampling limits generalization.** The sampler mixes signal-enriched,
   random, and procedural strata; the reporter pools them without inclusion
   weights and groups by era/source, not party/chamber. Its overall precision and
   recall are sample diagnostics, not automatically corpus-population estimates.
7. **Exposure and source changes are confounders.** Word weighting reflects
   prolific speakers and changing speech types. The Hein/GovInfo boundary,
   attribution coverage, OCR, and procedural filtering can shift rates without a
   behavioral shift. A party difference is descriptive, not causal.

[`METHODOLOGY.md`](METHODOLOGY.md) reports 784 model-assisted passages for the July
2026 v3 codebook, overall precision of at least 89.4% across published categories,
and personal-attack recall of only 16.5%. It also explicitly says the September
2026 v4 revision has not received a new blinded evaluation. Those are **documented
results, not independently reproduced here**: the generated validation artifacts
were not available in this checkout.

Even accepting those results, detecting roughly one in six annotated attacks is
not adequate support for an exhaustive "personal attack" measure. Do not simply
divide counts by 0.165: passage recall from an enriched sample is not a calibrated
correction factor for corpus hit counts or every historical subgroup.

**My assessment:** retain these as clearly labeled lexical-frequency series.
I would not presently trust them to rank sincerity, actual bipartisan behavior,
speaker-endorsed misconduct accusations, or cross-party hostility.

## 6. A better method: human-validated, context- and target-aware events

The simplest defensible starting point is a **probability-sampled human audit**,
not replacing the dictionaries with an unvalidated toxicity model. Automated
scaling should follow demonstrated validity, not precede it.

### Define events before training

Annotate a sentence/clause with access to the surrounding turn. Record evidence
spans, speaker, target, target type/party at that date, quotation/attribution,
endorsement, negation, and uncertainty. Allow multiple labels:

| Construct | Proposed rule |
|---|---|
| Formulaic address | Count conventional address as ritual; do not infer warmth. Track mentions of civility separately. |
| Substantive praise | Require an identifiable praised target; distinguish colleagues, own party, other party, and nonpolitical targets. |
| Cooperation | Distinguish an actual joint-work claim from an aspiration, invitation, failure, or institutional descriptor. Verify behavioral claims with legislative data when making claims about action. |
| Personal attack | Require an asserted attack on an identifiable person/group, excluding mere policy disagreement, quotation, and denial. |
| Misconduct allegation | Require an asserted allegation against an identifiable target; separate legal-topic discussion, denial, and reported accusations. Do not judge whether the allegation is true. |
| Ideological label / identity slur | Separate occurrence, target, and speaker endorsement; do not automatically equate ideological labeling with abuse. |
| Opposing-party event | Require a resolved target that is actually opposite the speaker's party; keep unresolved and independent cases distinct. |
| Party naming | Count `Democrat party` as a naming variant; annotate derogatory use separately instead of building intent into its name. |

### Build an independent reference set

Start with a pilot of approximately 2,000 real passages stratified by era,
source, chamber, and party. This is a planning budget, not a guarantee of enough
rare positives. Sample randomly for prevalence and missed expressions; oversample
rare signals for error analysis, recording inclusion probabilities. Expand the
sample wherever rare categories or subgroups remain too uncertain.

Use two trained human annotators, blinded to production scores and the comparison
hypothesis, plus independent human adjudication. Preserve disagreements and
`uncertain` outcomes. Give annotators enough context to resolve targets; reveal
speaker-party metadata only when needed for the party relationship.

Split development and locked evaluation sets by speaker and document/date to
avoid near-duplicate leakage. Add era/source holdouts to test historical transfer.
Keep the locked evaluation set out of lexicon tuning and model training.

### Scale only after measuring performance

Benchmark the current dictionaries, a simple supervised text classifier, and a
context-aware multi-label model on the same human labels. Select the least complex
method that meets the requirements. LLM-assisted coding can help create candidates,
but must not supply both the predictions and the supposedly independent truth.
Handle uncertain predictions explicitly; abstentions must not become negative
labels.

Keep lexical counts as a separate baseline. Persist each accepted event's
`turn_id`, character offsets, label, target, attribution, uncertainty, and
codebook/model version so published totals can be traced to source evidence.

### Publish estimands and uncertainty, not just lines

Report speaker-endorsed events per 1,000 eligible unquoted tokens and, separately,
opposing-party events per 100 resolved opposing-party reference events. Use a
consistent reference-event definition; do not let tone vocabulary define its own
denominator. Also report raw exposure, missing targets, abstentions, coverage, and
a separately labeled member-balanced view.

Probability-weighted human-audit estimates can be the primary evidence before a
scalable model is reliable. For automated totals, use a separate probability audit
to assess and, where justified, correct aggregate error. Report intervals accounting
for sampling, clustered speeches/members, and annotation/model uncertainty.
Do not present confidence in a model prediction as a confidence interval for a rate.

Evaluate event-level precision/recall, target resolution, quoted/endorsed
classification, and aggregate-rate error by party, chamber, era, and source.
One **proposed** release gate is lower 95% confidence bounds of at least 0.90
precision and 0.80 recall for each substantive category, with adequately powered
subgroup checks. These thresholds are project choices, not universal standards;
underpowered groups remain unvalidated. Publish sensitivity to alternative
thresholds and definitions, and withhold comparisons whose uncertainty does not
support the claimed difference.

### Practical rollout

First relabel the current outputs as lexical markers and show the targeting,
quotation, and validation limitations. Next complete the human audit and publish
weighted estimates alongside the legacy series. Only then introduce a versioned
context-aware scorer, independently evaluate it, and rescore consistently rather
than splicing new definitions into an old time series. Keep source-overlap
diagnostics and visibly separate sources when comparability is unsupported.

This proposal follows the task-specific validation emphasis of Grimmer and
Stewart, *Text as Data: The Promise and Pitfalls of Automatic Content Analysis
Methods for Political Texts* (2013), and the distinction between impolite style
and intolerant substance in Pendzel, Lotan, Zoizner, and Minkov, *A Closer Look at
Multidimensional Online Political Incivility* (EMNLP 2024). Neither paper validates
this repository or establishes that a social-media model transfers to historical
Congressional Record speech.

Source identifiers: `10.1093/pan/mps028` and `10.18653/v1/2024.emnlp-main.827`.
