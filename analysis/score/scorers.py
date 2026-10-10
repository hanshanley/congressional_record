"""Lexical scorers for congressional floor language (token-set fast path).

Loads the lexicons once and scores a turn's text for three measures:

* formulaic courtesy / deference (fuzzy-matched phrases such as "my distinguished colleague")
* profanity, by tier (mild/strong), from an exact high-precision codebook
* ethnic slurs used in the United States, from a separate exact codebook

Performance: each turn is tokenized **once** into lowercased word tokens. Single-word
lexicon terms are counted by O(1) set/dict membership; only genuinely multi-word
phrases fall back to regex. This keeps the full-corpus scan tractable.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

LEXDIR = Path(__file__).parent / "lexicons"

_MILD_PROFANITY_EXCLUSIONS = re.compile(
    r"\bto\s+damn\s+them\b"
    r"|\bbe\s+damned\s+and\s+annulled\b"
    r"|\block\s+and\s+damn\b"
    r"|\bkilled\s+a\s+damn\s+in\b"
    r"|\bmy\s+damn\s+sin\b"
    r"|\bcrap\s+game\b"
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
    parts = [r"\b" + r"\s+".join(frag(w) for w in p) + r"\b" for p in ordered]
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
    # "erected" into profanity). Ethnic slurs are kept in a separate exact list.
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


class Scorers:
    """Holds compiled lexicons; reused across all turns."""

    def __init__(self, fuzzy: bool = True) -> None:
        self.formal_courtesy = _Lexicon(_load_lines("formal_courtesy.txt"), fuzzy=fuzzy)
        self.profanity = _load_profanity()
        # Ethnic slurs are exact curated forms, separate from profanity, and never inflected.
        self.ethnic_slurs = _Lexicon(_load_lines("slurs.txt"), fuzzy=False)
        overlap = self.ethnic_slurs.singles & (
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

    def signal_spans(self, text: str) -> Dict[str, List[Tuple[int, int]]]:
        """Return scorer-accepted spans for deterministic validation sampling."""
        low = (text or "").lower()
        return {
            "formal_courtesy": self.formal_courtesy.find_spans(low),
            "profanity": sorted(
                self._mild_profanity_spans(low) + self.profanity["strong"].find_spans(low)
            ),
            "ethnic_slur": self.ethnic_slurs.find_spans(low),
        }

    def profanity_term_counts(self, text: str) -> Counter:
        """Return accepted, unquoted profanity surface forms and their counts.

        Callers are responsible for masking quotations first. This uses the same
        curated tiers and mild-term exclusions as ``score_turn``; ethnic slurs
        are counted separately and never as profanity.
        """
        low = (text or "").lower()
        strong = self.profanity["strong"].find_spans(low)
        return Counter(
            " ".join(low[start:end].split())
            for start, end in sorted(self._mild_profanity_spans(low) + strong)
        )

    def score_turn(self, text: str) -> Dict[str, int]:
        low = (text or "").lower()
        tokens = Counter(_TOKEN_RE.findall(low))
        mild = max(
            0,
            self.profanity["mild"].count(tokens, low)
            - len(_MILD_PROFANITY_EXCLUSIONS.findall(low)),
        )
        strong = self.profanity["strong"].count(tokens, low)
        return {
            "n_words": sum(tokens.values()),
            "formal_courtesy_hits": self.formal_courtesy.count(tokens, low),
            "profanity_mild": mild,
            "profanity_strong": strong,
            "profanity_hits": mild + strong,
            "ethnic_slur_hits": self.ethnic_slurs.count(tokens, low),
        }
