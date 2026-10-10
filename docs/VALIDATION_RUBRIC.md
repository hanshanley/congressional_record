# Model-assisted validation rubric

This validation uses **real Congressional Record passages only**. It is a disclosed
model-assisted consistency and face-validity check, not independent human ground truth.

Annotators receive `validation_sample_blinded.csv` without production metric values. For each
`sample_id`, label:

| Field | Allowed values | Rule |
|---|---|---|
| `formulaic_address` | yes/no/uncertain | Conventional parliamentary courtesy or address, regardless of substantive warmth. |
| `procedural_deference` | yes/no/uncertain | Courtesy required by floor procedure, yielding, recognition, or regular order. |
| `profanity` | yes/no/uncertain | Genuine curse/obscene expression, not neutral medical, sexual, religious, identity, or criminal vocabulary. |
| `ethnic_slur` | yes/no/uncertain | An ethnic slur occurs, not a homograph such as a name, place, or ordinary word. Occurrence does not imply endorsement. |
| `quoted_or_read_in` | yes/no/uncertain | Relevant language is quoted, read into the Record, condemned, or attributed to another source. |
| `ambiguous` | yes/no | Context is insufficient or supports materially different readings. |

Each pass must also record `confidence` (`low`, `medium`, `high`) and a concise `rationale`
grounded only in the supplied passage. Two passes are completed independently. A separate
adjudication pass sees both judgments and resolves disagreements without seeing production scores.
The blinded sample and finalized adjudication artifacts preserve `turn_id`, source, Congress,
and a SHA-256 hash of the exact passage; annotation labels remain joinable by `sample_id`.
