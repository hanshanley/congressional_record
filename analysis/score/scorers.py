"""Modular civility scorers (token-set fast path).

Loads the lexicons once and scores a turn's text for:

* comity/deference phrase hits (positive)
* formulaic courtesy, gratitude/praise, and bipartisan cooperation as separate components
* hostility/attack hits (negative)
* profanity hits by tier (mild/strong/slurs)
* out-group reference count (aisle idioms + high-precision opposing-party references,
  resolved to the speaker's party) and comity/hostility within a window around each
  reference (proximity context, not proof of direction)
* the "Democrat party" pejorative marker
* optional VADER sentiment compound

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

# Opposing-party name references, keyed by the SPEAKER's normalized party.
_OUTPARTY_TOKENS = {
    "D": {"republicans", "gop"},
    "R": {"democrat", "democrats"},
}
_OUTPARTY_PHRASES = {
    "D": re.compile(
        r"\brepublican\s+(?:party|colleagues?|members?|caucus|leadership|side|conference)\b"
    ),
    "R": re.compile(
        r"\bdemocratic\s+(?:party|colleagues?|members?|caucus|leadership|side|conference)\b"
    ),
}
# Matched only against already-lowercased text, so no re.IGNORECASE (which would be
# ~2.7x slower per scan across the 18.5M-turn corpus for identical results).
_DEMOCRAT_PARTY_PEJ = re.compile(r"\bdemocrat\s+party\b")
_COOPERATION_EXCLUSIONS = re.compile(
    r"\bin a bipartisan\s+(?:board|commission|committee)\b"
)
_HOSTILITY_EXCLUSIONS = re.compile(
    r"\bphony\s+(?:price|expense)\b"
    r"|\b(?:mental|legal)\s+incompetents?\b"
    r"|\bnot\s+(?:a\s+)?(?:coward|cowardly|liar|incompetent)\b"
    r"|\b(?:was|is)\s+(?:\w+\s+){0,4}a\s+liar\?"
)
_MISCONDUCT_EXCLUSIONS = re.compile(
    r"\bforeign\s+corrupt\s+practices\s+act\b"
    r"|\b(?:no|not|without)\s+(?:evidence\s+of\s+)?(?:corrupt|corruption)\b"
    r"|\bdo\s+not\s+believe\s+there\s+is\s+corruption\b"
)
_MISCONDUCT_NEGATION = re.compile(
    r"\b(?:no|not|without)"
    r"(?:\s+(?:any|credible|clear|direct|actual))?"
    r"(?:\s+(?:evidence|proof|finding|findings|sign|signs|allegation|allegations))?"
    r"(?:\s+of)?\s+$"
    r"|\bnot\s+guilty\s+of\s+$"
)
_MISCONDUCT_COORDINATION = re.compile(r"^\s*(?:,\s*)?(?:or|and)\s*$")
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
# Lightweight sentence splitter for sentence-level VADER (avoids an nltk dependency).
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


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
    # "erected" into profanity). Identity slurs are kept in a separate exact list.
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
    lex["slurs"] = _Lexicon(_load_lines("slurs.txt"), fuzzy=False)
    # De-duplicate surface forms across tiers so each token is counted once, in its most
    # severe tier. This protects against accidental duplicate surface forms in curated files.
    lex["strong"].singles -= lex["slurs"].singles
    lex["mild"].singles -= lex["slurs"].singles | lex["strong"].singles
    return lex


class Scorers:
    """Holds compiled lexicons; reused across all turns."""

    def __init__(self, use_sentiment: bool = False, fuzzy: bool = True) -> None:
        self.formal_courtesy = _Lexicon(_load_lines("formal_courtesy.txt"), fuzzy=fuzzy)
        self.gratitude_praise = _Lexicon(_load_lines("gratitude_praise.txt"), fuzzy=fuzzy)
        self.cooperation = _Lexicon(_load_lines("cooperation.txt"), fuzzy=fuzzy)
        self.hostility = _Lexicon(_load_lines("hostility.txt"), fuzzy=fuzzy)
        # Misconduct terms are exact curated forms; suffix expansion creates legal-topic
        # false positives (e.g. "corrupting" an abstract process).
        self.misconduct = _Lexicon(_load_lines("misconduct.txt"), fuzzy=False)
        self.ideological_labels = _Lexicon(_load_lines("ideological_labels.txt"), fuzzy=fuzzy)
        self.outgroup_idiom = _Lexicon(_load_lines("outgroup.txt"), fuzzy=fuzzy)
        self.profanity = _load_profanity()
        self._sid = None
        if use_sentiment:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

            self._sid = SentimentIntensityAnalyzer()

    def _sentiment(self, text: str) -> Tuple[float, float, int]:
        """Return (mean sentence compound, mean sentence negative share, sentence count).

        VADER is calibrated on sentence-length text and its ``compound`` score
        saturates on long passages, so scoring a whole speech (or a 5,000-char
        truncation of it) is biased. We instead split into sentences, score each,
        and average — the granularity VADER is designed for. The sentence count is
        returned so the aggregate can length-weight the per-turn mean (matching the
        word-weighting used for every other metric).
        """
        assert self._sid is not None
        sentences = [s for s in _SENTENCE_RE.split(text) if s.strip()]
        if not sentences:
            return 0.0, 0.0, 0
        comp = neg = 0.0
        for s in sentences:
            sc = self._sid.polarity_scores(s)
            comp += sc["compound"]
            neg += sc["neg"]
        n = len(sentences)
        return comp / n, neg / n, n

    @staticmethod
    def _count_excluding(
        lexicon: _Lexicon,
        tokens: Counter,
        text: str,
        exclusions: re.Pattern,
    ) -> int:
        return max(0, lexicon.count(tokens, text) - len(exclusions.findall(text)))

    @staticmethod
    def _without_excluded_spans(
        spans: List[Tuple[int, int]], text: str, exclusions: re.Pattern
    ) -> List[Tuple[int, int]]:
        blocked = [match.span() for match in exclusions.finditer(text)]
        return [
            span for span in spans
            if not any(span[0] < end and start < span[1] for start, end in blocked)
        ]

    def _misconduct_spans(
        self,
        text: str,
        token_spans: Optional[List[Tuple[str, int, int]]] = None,
    ) -> List[Tuple[int, int]]:
        spans = self._without_excluded_spans(
            self.misconduct.find_spans(text, token_spans),
            text,
            _MISCONDUCT_EXCLUSIONS,
        )
        accepted: List[Tuple[int, int]] = []
        previous_negated: Optional[Tuple[int, int]] = None
        for span in spans:
            directly_negated = bool(
                _MISCONDUCT_NEGATION.search(text[max(0, span[0] - 80):span[0]])
            )
            coordinated_negation = (
                previous_negated is not None
                and bool(_MISCONDUCT_COORDINATION.fullmatch(
                    text[previous_negated[1]:span[0]]
                ))
            )
            if directly_negated or coordinated_negation:
                previous_negated = span
            else:
                accepted.append(span)
                previous_negated = None
        return accepted

    @staticmethod
    def _window_text(text: str, spans: List[Tuple[int, int]], radius: int = 200) -> str:
        """Concatenate ±``radius``-char windows around each span (merged intervals).

        Fragments are joined with a non-word, non-whitespace sentinel (ASCII RS) so that a
        phrase can never match *across* a window boundary — text that is not contiguous in
        the source: the phrase regex's ``\\s+`` / ``\\b`` cannot span the sentinel.
        """
        if not spans:
            return ""
        ivs = sorted((max(0, s - radius), min(len(text), e + radius)) for s, e in spans)
        merged: List[List[int]] = [list(ivs[0])]
        for s, e in ivs[1:]:
            if s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        return " \x1e ".join(text[s:e] for s, e in merged)

    @staticmethod
    def _reference_context(
        text: str, span: Tuple[int, int], max_radius: int = 300
    ) -> str:
        """Sentence/clause containing one target reference, bounded for OCR run-ons."""
        start, end = span
        left_bound = max(0, start - max_radius)
        right_bound = min(len(text), end + max_radius)
        left_fragment = text[left_bound:start]
        right_fragment = text[end:right_bound]
        left_breaks = [left_fragment.rfind(char) for char in ".!?;\n"]
        left = left_bound + max(left_breaks) + 1 if max(left_breaks) >= 0 else left_bound
        right_positions = [
            pos for char in ".!?;\n"
            if (pos := right_fragment.find(char)) >= 0
        ]
        right = end + min(right_positions) + 1 if right_positions else right_bound
        return text[left:right]

    def _idiom_spans(self, text_lower: str) -> List[Tuple[int, int]]:
        if self.outgroup_idiom.phrase_re is None:
            return []
        return [m.span() for m in self.outgroup_idiom.phrase_re.finditer(text_lower)]

    @staticmethod
    def _distinct_spans(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """Collapse overlapping detectors so one reference is counted once."""
        if not spans:
            return []
        merged = [list(span) for span in sorted(spans)]
        out = [merged[0]]
        for start, end in merged[1:]:
            if start < out[-1][1]:
                out[-1][1] = max(out[-1][1], end)
            else:
                out.append([start, end])
        return [(start, end) for start, end in out]

    def _outgroup_spans(self, text_lower: str, party: str) -> List[Tuple[int, int]]:
        outtok = _OUTPARTY_TOKENS.get(party)
        token_spans = [
            match.span()
            for match in _TOKEN_RE.finditer(text_lower)
            if outtok and match.group() in outtok
        ]
        phrase_re = _OUTPARTY_PHRASES.get(party)
        phrase_spans = (
            [match.span() for match in phrase_re.finditer(text_lower)]
            if phrase_re else []
        )
        return self._distinct_spans(self._idiom_spans(text_lower) + token_spans + phrase_spans)

    def signal_spans(self, text: str, party: str) -> Dict[str, List[Tuple[int, int]]]:
        """Return scorer-accepted spans for deterministic validation sampling."""
        low = (text or "").lower()
        cooperation = self._without_excluded_spans(
            self.cooperation.find_spans(low), low, _COOPERATION_EXCLUSIONS
        )
        hostility = self._without_excluded_spans(
            self.hostility.find_spans(low), low, _HOSTILITY_EXCLUSIONS
        )
        mild = self._without_excluded_spans(
            self.profanity["mild"].find_spans(low), low, _MILD_PROFANITY_EXCLUSIONS
        )
        return {
            "formal_courtesy": self.formal_courtesy.find_spans(low),
            "gratitude_praise": self.gratitude_praise.find_spans(low),
            "cooperation": cooperation,
            "personal_attack": hostility,
            "misconduct_allegation": self._misconduct_spans(low),
            "profanity": sorted(mild + self.profanity["strong"].find_spans(low)),
            "identity_slur": self.profanity["slurs"].find_spans(low),
            "outparty_target": self._outgroup_spans(low, party),
        }

    def profanity_term_counts(self, text: str) -> Counter:
        """Return accepted, unquoted profanity surface forms and their counts.

        Callers are responsible for masking quotations first. This uses the same
        curated tiers and mild-term exclusions as ``score_turn``; identity slurs
        remain excluded from profanity.
        """
        low = (text or "").lower()
        mild = self._without_excluded_spans(
            self.profanity["mild"].find_spans(low), low, _MILD_PROFANITY_EXCLUSIONS
        )
        strong = self.profanity["strong"].find_spans(low)
        return Counter(
            " ".join(low[start:end].split())
            for start, end in sorted(mild + strong)
        )

    def score_turn(self, text: str, party: str) -> Dict[str, float]:
        text = text or ""
        low = text.lower()
        # Single tokenization pass: build the token Counter, the word count, AND the
        # out-party name spans from one walk over the matches (no redundant re-scan).
        outtok = _OUTPARTY_TOKENS.get(party)
        tokens: Counter = Counter()
        outparty_spans: List[Tuple[int, int]] = []
        misconduct_token_spans: List[Tuple[str, int, int]] = []
        n_words = 0
        for m in _TOKEN_RE.finditer(low):
            g = m.group()
            tokens[g] += 1
            if g in self.misconduct.singles:
                misconduct_token_spans.append((g, *m.span()))
            n_words += 1
            if outtok and g in outtok:
                outparty_spans.append(m.span())

        phrase_re = _OUTPARTY_PHRASES.get(party)
        phrase_spans = [m.span() for m in phrase_re.finditer(low)] if phrase_re else []
        spans = self._distinct_spans(self._idiom_spans(low) + outparty_spans + phrase_spans)

        prof = {tier: lex.count(tokens, low) for tier, lex in self.profanity.items()}
        prof["mild"] = max(
            0, prof.get("mild", 0) - len(_MILD_PROFANITY_EXCLUSIONS.findall(low))
        )
        formal_courtesy_hits = self.formal_courtesy.count(tokens, low)
        gratitude_praise_hits = self.gratitude_praise.count(tokens, low)
        cooperation_hits = self._count_excluding(
            self.cooperation, tokens, low, _COOPERATION_EXCLUSIONS
        )
        hostility_hits = self._count_excluding(
            self.hostility, tokens, low, _HOSTILITY_EXCLUSIONS
        )
        misconduct_hits = len(self._misconduct_spans(low, misconduct_token_spans))
        comity_hits = formal_courtesy_hits + gratitude_praise_hits + cooperation_hits
        win = self._window_text(low, spans)
        win_tokens = Counter(_TOKEN_RE.findall(win)) if win else Counter()
        reference_contexts = []
        for span in spans:
            context = self._reference_context(low, span)
            context_tokens = Counter(_TOKEN_RE.findall(context))
            reference_contexts.append((context, context_tokens))

        win_formal = self.formal_courtesy.count(win_tokens, win)
        win_gratitude = self.gratitude_praise.count(win_tokens, win)
        win_cooperation = self._count_excluding(
            self.cooperation, win_tokens, win, _COOPERATION_EXCLUSIONS
        )

        out: Dict[str, float] = {
            "n_words": n_words,
            "comity_hits": comity_hits,
            "formal_courtesy_hits": formal_courtesy_hits,
            "gratitude_praise_hits": gratitude_praise_hits,
            "cooperation_hits": cooperation_hits,
            "hostility_hits": hostility_hits,
            "misconduct_hits": misconduct_hits,
            "ideological_label_hits": self.ideological_labels.count(tokens, low),
            "profanity_mild": prof.get("mild", 0),
            "profanity_strong": prof.get("strong", 0),
            "profanity_slurs": prof.get("slurs", 0),
            # Identity slurs are a separate context-audited category, not profanity.
            "profanity_hits": prof.get("mild", 0) + prof.get("strong", 0),
            "outgroup_refs": len(spans),
            # Only run the pejorative regex when the token "democrat" is present.
            "democrat_party_pej": len(_DEMOCRAT_PARTY_PEJ.findall(low)) if "democrat" in tokens else 0,
            "directed_comity_hits": win_formal + win_gratitude + win_cooperation,
            "directed_hostility_hits": self._count_excluding(
                self.hostility, win_tokens, win, _HOSTILITY_EXCLUSIONS
            ),
            "directed_misconduct_hits": len(self._misconduct_spans(win)),
            "outgroup_comity_contexts": sum(
                (
                    self.formal_courtesy.count(context_tokens, context)
                    + self.gratitude_praise.count(context_tokens, context)
                    + self._count_excluding(
                        self.cooperation,
                        context_tokens,
                        context,
                        _COOPERATION_EXCLUSIONS,
                    )
                ) > 0
                for context, context_tokens in reference_contexts
            ),
            "outgroup_hostility_contexts": sum(
                self._count_excluding(
                    self.hostility, context_tokens, context, _HOSTILITY_EXCLUSIONS
                ) > 0
                for context, context_tokens in reference_contexts
            ),
            "outgroup_misconduct_contexts": sum(
                len(self._misconduct_spans(context)) > 0
                for context, context_tokens in reference_contexts
            ),
        }
        if self._sid is not None:
            compound, neg_share, n_sentences = self._sentiment(text)
            out["sentiment"] = compound
            out["neg_share"] = neg_share
            out["n_sentences"] = n_sentences
        return out
