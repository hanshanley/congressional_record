"""Lexical scorers for congressional floor language (token-set fast path).

Loads the lexicons once and scores a turn's text for three measures:

* formulaic courtesy / deference (fuzzy-matched phrases such as "my distinguished colleague")
* profanity, by tier (mild/strong), from an exact high-precision codebook
* ethnic, sexual-orientation, gender-identity, and disability slurs used in the United
  States, from a separate exact codebook with context exclusions for homographs

Performance: each turn is tokenized **once** into lowercased word tokens. Single-word
lexicon terms are counted by O(1) set/dict membership; only genuinely multi-word
phrases fall back to regex. This keeps the full-corpus scan tractable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

LEXDIR = Path(__file__).parent / "lexicons"

_MILD_PROFANITY_EXCLUSIONS = re.compile(
    r"\bto\s+damn\s+them\b"
    r"|\bbe\s+damned\s+and\s+annulled\b"
    r"|\block\s+and\s+damn\b"
    r"|\bkilled\s+a\s+damn\s+in\b"
    r"|\bmy\s+damn\s+sin\b"
    r"|\bcrap\s+game\b"
    r"|\bbuilding\s+this\s+damn(?=\s*[,.;:])"
    r"|\bcrap\s{1,12}(?:games|tables?|shoot(?:s|ing|ers?)?)\b"
    r"|\bhunt\s{1,12}buggers,\s{1,12}as\s{1,12}they\s{1,12}used\s{1,12}to"
    r"\s{1,12}say;\s{1,12}buggers\s{1,12}or\s{1,12}monsters\b"
    r"|\bbuggers\s{1,12}or\s{1,12}monsters\b"
    r"|\bwooll?y\s{1,12}buggers?\b"
    r"|\bburst\s{1,12}the\s{1,12}damn\s{1,12}and\s{1,12}flooded\b"
    r"|\bdamn\s{1,12}removal\s{1,12}will\s{1,12}irreversibly"
    r"\s{1,12}remove\s{1,12}jobs\b"
    r"|\bthe\s{1,12}best\s{1,12}damn\s{1,12}band\s{1,12}in\s{1,12}the"
    r"\s{1,12}land\b"
    r"|\bblaze\s{1,12}damned\s{1,12}more\s{1,12}than\b"
    r"|\bb-17,\s{1,12}the\s{1,12}damn\s{1,12}yankee\b"
    r"|\bbuettner,\s{1,12}ruth;\s{1,12}bugger,\s{1,12}doug\b"
    r"|\bcaseworker,\s{1,12}doug\s{1,12}bugger\b"
)
_STRONG_PROFANITY_EXCLUSIONS = re.compile(
    r"\bwilliam\s+the\s+bastard\b"
    r"|\bto\s+asses\s+(?:the|a|an)\b"
    r"|\b(?:to|should|must|will|would|can|could|may)\s{1,12}asses\s{1,12}"
    r"(?:the|a|an|all|their|our|that|this)\b"
    r"|\binterpret\s{1,12}and\s{1,12}asses\s{1,12}that\b"
    r"|\bbastard\s{1,12}lata(?:['\u2019]s|s)?\b"
    r"|\bbattling\s{1,12}bastards\s{1,12}of\s{1,12}bataan\b"
    r"|\briding\s{1,12}a\s{1,12}jackass\b"
    r"|\bsuch\s{1,12}ass\s{1,12}loons\b"
    r"|\box\s{1,12}or\s{1,12}(?:the\s{1,12})?ass\b"
    r"|\bto\s{1,12}asses\s{1,12}or\s{1,12}challenge\b"
    r"|\baccurately\s{1,12}asses\s{1,12}both\s{1,12}bases\b"
    r"|\bto\s{1,12}asses\s{1,12}such\s{1,12}injuries\b"
    r"|\bto\s{1,12}asses\s{1,12}its\s{1,12}effectiveness\b"
    r"|\bhelp\s{1,12}them\s{1,12}asses\s{1,12}their\s{1,12}own\b"
    r"|\bmichael\s{1,12}oxley\s{1,12}ass\s{1,12}the\s{1,12}sarbanes-oxley\b"
    r"|\bbattered\s{1,12}bastards\s{1,12}of\s{1,12}bastogne\b"
)
# Slur forms that double as other words. A slur span overlapping one of these is not counted.
_SLUR_EXCLUSIONS = re.compile(
    # "retard" as a verb: after a modal/auxiliary/adverb, or followed by an object.
    r"\b(?:to|will|would|may|might|could|can|cannot|should|must|shall|not|does|did|do"
    r"|tend|tends|further|greatly|seriously|materially|thus|only|also|merely|but)\s+retard\b"
    r"|\bretards?\s+(?:the|its|their|our|his|her|a|an|this|that|these|those|any|all|such"
    r"|economic|growth|progress|development|recovery|investment|production|commerce|trade"
    r"|competition|innovation|it|them|us|rather|instead)\b"
    # Archaic "faggot", a bundle of sticks, and 19th-century "faggot votes".
    r"|\bfaggots?\s+(?:of|votes?|voters?|voting)\b"
    r"|\b(?:sticks|wood|brush|fuel|bundles?)\s+(?:and\s+)?faggots?\b"
    r"|\bfag\s+ends?\b"
    r"|\bhomo\s+(?:sapiens|erectus|habilis|economicus|faber|neanderthalensis|novus|politicus"
    r"|ludens|homini)\b|\becce\s+homo\b"
    # Line-break hyphenation in the printed Record ("homo- geneous", "re- tard- ed").
    r"|\b[a-z]+-\s+[a-z]"
    r"|\b[a-z]+\s*-\s*\n"
)
# Tokenizer: word tokens keep internal hyphens/apostrophes (un-american, don't).
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-'\u2019][a-z0-9]+)*")


def _load_lines(name: str) -> List[str]:
    out: List[str] = []
    for line in (LEXDIR / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# Function/short words in phrases that should NOT be inflected.
_FUNCTION = {
    "the", "a", "an", "my", "our", "your", "his", "her", "their", "of", "from",
    "to", "on", "in", "for", "and", "i", "both", "that", "this", "with", "at",
    "by", "side", "sides", "aisle", "other", "no",
}
# Irregular plurals/forms common in parliamentary address that suffix rules miss.
_IRREGULAR = {
    "gentleman": {"gentlemen"},
    "gentlewoman": {"gentlewomen"},
    "gentlelady": {"gentleladies"},
    "woman": {"women"},
    "man": {"men"},
    "lady": {"ladies"},
}


def _regular_variants(w: str) -> Set[str]:
    """Common English inflections of ``w`` (plurals + verb forms) via suffix rules."""
    v = {w}
    if w.endswith(("s", "x", "z", "ch", "sh")):
        v.add(w + "es")
    else:
        v.add(w + "s")
    if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
        v.add(w[:-1] + "ies")
    if w.endswith("e"):
        v.add(w + "d")            # like -> liked
        v.add(w[:-1] + "ing")     # commit-e? handle simple: use -> using
    else:
        v.add(w + "ed")
        v.add(w + "ing")
    return v


def morph_variants(word: str) -> Set[str]:
    """All fuzzy-match variants of a single lexicon word (regular + irregular)."""
    out = _regular_variants(word)
    out |= _IRREGULAR.get(word, set())
    return out


def plural_variants(word: str) -> Set[str]:
    """Plural/irregular variants of a single word (retained for tests).

    Not used by the production phrase path any more — phrases inflect every content word
    inline via :func:`_word_regex`. Kept as a small standalone helper the test-suite pins.
    """
    v = {word}
    if word.endswith(("s", "x", "z", "ch", "sh")):
        v.add(word + "es")
    elif word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
        v.add(word[:-1] + "ies")
    else:
        v.add(word + "s")
    v |= _IRREGULAR.get(word, set())
    return v


# Minimum length for a token to be treated as an inflectable "content" word.
# Shorter tokens are matched literally so obfuscation stubs (e.g. "len") are never
# expanded into ordinary English words (e.g. "lens").
_MIN_INFLECT_LEN = 4


def _word_regex(w: str) -> str:
    """Regex fragment matching a content word and all its inflections.

    Derived from :func:`morph_variants` (an alternation of its escaped forms, longest
    first) so the English suffix rules live in exactly ONE place — the set-builders and
    this regex-builder can never drift apart. Emitting one such group per content word
    lets a phrase inflect the correct token regardless of position (so "reach across the
    aisle" matches "reaches/reached across the aisle", where the inflected word is the
    leading verb, not the trailing noun).

    Forms are ordered longest-first with a lexicographic tiebreak. ``morph_variants``
    returns a set, whose iteration order varies with per-process hash randomisation;
    without the tiebreak, equal-length forms would be emitted in a different order on
    every run. Because Python alternation is leftmost-first rather than longest-match,
    that ordering is load-bearing and must be deterministic.
    """
    forms = sorted(morph_variants(w), key=lambda f: (-len(f), f))
    return "(?:" + "|".join(re.escape(f) for f in forms) + ")"


def _split_terms(terms: List[str], fuzzy: bool = True) -> Tuple[Set[str], List[Tuple[str, ...]]]:
    """Partition terms into a single-word set and multi-word token-tuple list.

    When ``fuzzy`` is set, single words (>= 4 chars) are expanded with morphological
    variants so plurals/verb-forms ("colleague" -> "colleagues") match. Phrases are kept
    as raw token tuples; their inflection is handled inline by :func:`_phrase_regex`.
    """
    singles: Set[str] = set()
    phrases: List[Tuple[str, ...]] = []
    for t in terms:
        toks = _TOKEN_RE.findall(t.lower())
        if len(toks) == 1:
            expand = fuzzy and len(toks[0]) >= _MIN_INFLECT_LEN
            singles |= morph_variants(toks[0]) if expand else {toks[0]}
        elif len(toks) > 1:
            phrases.append(tuple(toks))
    return singles, phrases


def _phrase_regex(phrases: List[Tuple[str, ...]], fuzzy: bool = True) -> Optional[re.Pattern]:
    if not phrases:
        return None

    def frag(w: str) -> str:
        if fuzzy and w not in _FUNCTION and len(w) >= _MIN_INFLECT_LEN:
            return _word_regex(w)
        return re.escape(w)

    # Deduplicate, then order longest phrase first with a lexicographic tiebreak.
    # `set` iteration order varies with per-process hash randomisation, and Python
    # alternation is leftmost-first rather than longest-match, so an unordered
    # alternation makes the counts depend on the process. Longest-first also resolves
    # genuine ambiguity in favour of the more specific phrase.
    ordered = sorted(set(phrases), key=lambda p: (-len(" ".join(p)), p))
    # Match whole tokenizer words, not a phrase fragment inside an attached compound.
    parts = [
        r"\b(?<![a-z0-9][-'’])" + r"\s+".join(frag(w) for w in p)
        + r"\b(?![-'’][a-z0-9])"
        for p in ordered
    ]
    # No re.IGNORECASE: patterns are built from lowercased tokens and only ever matched
    # against lowercased text (`low`/`win`), so the flag is pure overhead (~2.7x/scan).
    return re.compile("|".join(parts))


class _Lexicon:
    """A single lexicon: fast single-word set + optional multi-word regex."""

    def __init__(self, terms: List[str], fuzzy: bool = True) -> None:
        self.singles, phrases = _split_terms(terms, fuzzy=fuzzy)
        self.phrase_re = _phrase_regex(phrases, fuzzy=fuzzy)

    def count(self, tokens: Counter, text: str) -> int:
        # Intersect with the (usually small) token set instead of scanning all singles.
        n = sum(tokens[w] for w in self.singles.intersection(tokens))
        if self.phrase_re is not None and text:
            matches = list(self.phrase_re.finditer(text))
            n += len(matches)
            # A phrase may contain a term that is also a single-word entry. Subtract only
            # the ACTUAL single-token matches inside the phrase span, rather than inferred
            # variant overlap (which could zero one phrase and double another).
            for match in matches:
                n -= sum(1 for w in _TOKEN_RE.findall(match.group()) if w in self.singles)
        return n

    def find_spans(
        self,
        text: str,
        token_spans: Optional[List[Tuple[str, int, int]]] = None,
    ) -> List[Tuple[int, int]]:
        """Return de-duplicated surface spans using the same matching rules as ``count``."""
        phrase_spans = (
            [match.span() for match in self.phrase_re.finditer(text)]
            if self.phrase_re is not None and text else []
        )
        tokens = token_spans
        if tokens is None:
            tokens = [(match.group(), *match.span()) for match in _TOKEN_RE.finditer(text)]
        single_spans = [
            (start, end)
            for token, start, end in tokens
            if token in self.singles
            and not any(p_start <= start and end <= p_end for p_start, p_end in phrase_spans)
        ]
        return sorted(phrase_spans + single_spans)


def _load_profanity() -> Dict[str, "_Lexicon"]:
    # Profanity uses an explicitly enumerated high-precision list: do not generate
    # morphology (the former broad list turned ordinary words such as "strips" and
    # "erected" into profanity). Slurs are kept in a separate exact list.
    tiers: Dict[str, List[str]] = {"mild": [], "strong": []}
    for line in _load_lines("profanity.txt"):
        term, _, tier = line.partition("\t")
        term, tier = term.strip(), tier.strip()
        if not term or tier not in tiers:
            raise ValueError(
                "profanity.txt rows must use '<term>\\t<mild|strong>': "
                f"{line!r}"
            )
        tiers[tier].append(term)
    lex = {tier: _Lexicon(terms, fuzzy=False) for tier, terms in tiers.items()}
    # De-duplicate surface forms across tiers so each token is counted once, in its most
    # severe tier. This protects against accidental duplicate surface forms in curated files.
    lex["mild"].singles -= lex["strong"].singles
    return lex


class _ProfanityContext(NamedTuple):
    start: int
    end: int
    form: str
    tier: str
    matched_text: str


def _load_profanity_contexts(records: Optional[List[dict]] = None):
    if records is None:
        payload = json.loads((LEXDIR / "profanity_contexts.json").read_text(encoding="utf-8"))
        if type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("Unsupported profanity context schema")
        records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Profanity context records must be a list")
    tiers = {line.split("\t")[0]: line.split("\t")[1] for line in _load_lines("profanity.txt")}
    contexts: Dict[str, List[_ProfanityContext]] = {}
    seen = set()
    for row in records:
        digest = row["text_sha256"]
        start, end = row["start"], row["end"]
        form, tier, matched = row["form"], row["tier"], row["matched_text"]
        if (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(start) is not int or type(end) is not int or not 0 <= start < end
            or not isinstance(matched, str) or len(matched) != end - start
            or " ".join(matched.lower().split()) != form or tiers.get(form) != tier
            or not isinstance(row.get("source_turn_id"), str) or not row["source_turn_id"].strip()
            or not isinstance(row.get("reason"), str) or not row["reason"].strip()
        ):
            raise ValueError(f"Invalid reviewed profanity context: {row}")
        identity = (digest, start, end)
        if identity in seen:
            raise ValueError(f"Duplicate reviewed profanity context: {identity}")
        seen.add(identity)
        contexts.setdefault(digest, []).append(_ProfanityContext(start, end, form, tier, matched))
    return contexts


class Scorers:
    """Holds compiled lexicons; reused across all turns."""

    def __init__(self, fuzzy: bool = True, profanity_contexts: Optional[List[dict]] = None) -> None:
        self.formal_courtesy = _Lexicon(_load_lines("formal_courtesy.txt"), fuzzy=fuzzy)
        self.profanity = _load_profanity()
        self.profanity_contexts = _load_profanity_contexts(profanity_contexts)
        # Slurs are exact curated forms, separate from profanity, and never inflected.
        self.slurs = _Lexicon(_load_lines("slurs.txt"), fuzzy=False)
        overlap = self.slurs.singles & (
            self.profanity["mild"].singles | self.profanity["strong"].singles
        )
        if overlap:
            raise ValueError(f"forms listed as both profanity and slurs: {sorted(overlap)}")

    def _mild_profanity_spans(self, low: str) -> List[Tuple[int, int]]:
        blocked = [match.span() for match in _MILD_PROFANITY_EXCLUSIONS.finditer(low)]
        return [
            span for span in self.profanity["mild"].find_spans(low)
            if not any(span[0] < end and start < span[1] for start, end in blocked)
        ]

    def _slur_spans(self, low: str) -> List[Tuple[int, int]]:
        blocked = [match.span() for match in _SLUR_EXCLUSIONS.finditer(low)]
        return [
            span for span in self.slurs.find_spans(low)
            if not any(span[0] < end and start < span[1] for start, end in blocked)
        ]

    def _strong_profanity_spans(self, low: str) -> List[Tuple[int, int]]:
        blocked = [match.span() for match in _STRONG_PROFANITY_EXCLUSIONS.finditer(low)]
        return [
            span for span in self.profanity["strong"].find_spans(low)
            if not any(span[0] < end and start < span[1] for start, end in blocked)
        ]

    def _slur_count(self, tokens: Counter, low: str) -> int:
        count = self.slurs.count(tokens, low)
        # Exclusion contexts are rare, so only pay for span matching when a slur matched.
        return len(self._slur_spans(low)) if count else 0

    def reviewed_profanity_spans(
        self, text: str, *, lower_offsets: bool = False,
    ) -> Dict[str, List[Tuple[int, int]]]:
        """Content-addressed decisions preserve original words and their denominator."""
        blocked: Dict[str, List[Tuple[int, int]]] = {"mild": [], "strong": []}
        if not self.profanity_contexts:
            return blocked
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        lexical_spans = {}
        for row in self.profanity_contexts.get(digest, []):
            if text[row.start:row.end] != row.matched_text:
                raise ValueError(f"Reviewed profanity context no longer matches: {digest}")
            span = (len(text[:row.start].lower()), len(text[:row.end].lower()))
            if row.tier not in lexical_spans:
                lexical_spans[row.tier] = set(self.profanity[row.tier].find_spans(text.lower()))
            if span not in lexical_spans[row.tier]:
                raise ValueError(f"Reviewed exclusion is not a complete codebook occurrence: {digest}")
            blocked[row.tier].append(span if lower_offsets else (row.start, row.end))
        return blocked

    def _accepted_profanity_spans(self, text: str) -> Dict[str, List[Tuple[int, int]]]:
        low = (text or "").lower()
        spans = {
            "mild": self._mild_profanity_spans(low),
            "strong": self._strong_profanity_spans(low),
        }
        if not any(spans.values()):
            return spans
        blocked = self.reviewed_profanity_spans(text, lower_offsets=True)
        return {
            tier: [
                span for span in matches
                if not any(span[0] < end and start < span[1] for start, end in blocked[tier])
            ]
            for tier, matches in spans.items()
        }

    def signal_spans(self, text: str) -> Dict[str, List[Tuple[int, int]]]:
        """Return scorer-accepted spans for deterministic validation sampling."""
        low = (text or "").lower()
        profanity = self._accepted_profanity_spans(text)
        return {
            "formal_courtesy": self.formal_courtesy.find_spans(low),
            "profanity": sorted(
                profanity["mild"] + profanity["strong"]
            ),
            "slur": self._slur_spans(low),
        }

    def profanity_term_counts(self, text: str) -> Counter:
        """Return accepted, unquoted profanity surface forms and their counts.

        Callers are responsible for masking quotations first. This uses the same
        curated tiers and mild-term exclusions as ``score_turn``; slurs
        are counted separately and never as profanity.
        """
        low = (text or "").lower()
        spans = self._accepted_profanity_spans(text)
        return Counter(
            " ".join(low[start:end].split())
            for start, end in sorted(spans["mild"] + spans["strong"])
        )

    def score_turn(self, text: str) -> Dict[str, int]:
        low = (text or "").lower()
        tokens = Counter(_TOKEN_RE.findall(low))
        mild = self.profanity["mild"].count(tokens, low)
        if mild and _MILD_PROFANITY_EXCLUSIONS.search(low):
            mild = len(self._mild_profanity_spans(low))
        strong = self.profanity["strong"].count(tokens, low)
        if strong and _STRONG_PROFANITY_EXCLUSIONS.search(low):
            strong = len(self._strong_profanity_spans(low))
        if (mild or strong) and self.profanity_contexts:
            reviewed = self.reviewed_profanity_spans(text)
            if any(reviewed.values()):
                spans = self._accepted_profanity_spans(text)
                mild, strong = len(spans["mild"]), len(spans["strong"])
        return {
            "n_words": sum(tokens.values()),
            "formal_courtesy_hits": self.formal_courtesy.count(tokens, low),
            "profanity_mild": mild,
            "profanity_strong": strong,
            "profanity_hits": mild + strong,
            "slur_hits": self._slur_count(tokens, low),
        }
