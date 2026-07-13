# Model-assisted validation rubric

This validation uses **real Congressional Record passages only**. It is a disclosed
model-assisted consistency and face-validity check, not independent human ground truth.

Annotators receive `validation_sample_blinded.csv` without production metric values. For each
`sample_id`, label:

| Field | Allowed values | Rule |
|---|---|---|
| `target_exists` | yes/no/uncertain | A person, party, caucus, or clearly identified group is the object of the relevant language. |
| `outparty_target_exists` | yes/no/uncertain | The passage explicitly refers to the party opposing the recorded speaker's party. Do not infer from ideology alone. |
| `target_party` | D/R/I/other/none/uncertain | Party of the target when text supports it; never infer from ideology alone. |
| `formulaic_address` | yes/no/uncertain | Conventional parliamentary courtesy or address, regardless of substantive warmth. |
| `procedural_deference` | yes/no/uncertain | Courtesy required by floor procedure, yielding, recognition, or regular order. |
| `gratitude_praise` | yes/no/uncertain | Genuine thanks, praise, appreciation, commendation, or respect. |
| `bipartisan_cooperation` | yes/no/uncertain | Explicit shared work, compromise, common ground, or cross-party cooperation. |
| `personal_attack` | yes/no/uncertain | Attack on a person's honesty, integrity, competence, character, or fitness. |
| `misconduct_allegation` | yes/no/uncertain | Text alleges illegal, corrupt, unethical, or abusive conduct; this never establishes that conduct occurred. |
| `ideological_label` | yes/no/uncertain | Ideological categorization such as socialist, communist, fascist, or authoritarian. |
| `profanity` | yes/no/uncertain | Genuine curse/obscene expression, not neutral medical, sexual, religious, identity, or criminal vocabulary. |
| `identity_slur` | yes/no/uncertain | Identity-directed slur occurs. Occurrence does not imply endorsement. |
| `quoted_or_read_in` | yes/no/uncertain | Relevant language is quoted, read into the Record, condemned, or attributed to another source. |
| `ambiguous` | yes/no | Context is insufficient or supports materially different readings. |

Each pass must also record `confidence` (`low`, `medium`, `high`) and a concise `rationale`
grounded only in the supplied passage. Two passes are completed independently. A separate
adjudication pass sees both judgments and resolves disagreements without seeing production scores.
All files preserve `turn_id`, source, Congress, and a SHA-256 hash of the exact passage.
