"""Modular civility scorers (token-set fast path).

Loads the lexicons once and scores a turn's text for:

* comity/deference phrase hits (positive)
* hostility/attack hits (negative)
* profanity hits by tier (mild/strong/slurs)
* out-group reference count (aisle idioms + opposing-party name, resolved to the
  speaker's party) and **directed** comity/hostility within a window around each
  out-group reference ("civility toward the other party")
* the "Democrat party" pejorative marker
* optional VADER sentiment compound

Performance: each turn is tokenized **once** into lowercased word tokens. Single-word
lexicon terms are counted by O(1) set/dict membership; only genuinely multi-word
phrases fall back to regex. This keeps the full-corpus scan tractable.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

LEXDIR = Path(__file__).parent / "lexicons"

# Opposing-party name references, keyed by the SPEAKER's normalized party.
_OUTPARTY_TOKENS = {
    "D": {"republican", "republicans", "gop"},
    "R": {"democrat", "democrats", "democratic"},
}
# Matched only against already-lowercased text, so no re.IGNORECASE (which would be
# ~2.7x slower per scan across the 18.5M-turn corpus for identical results).
_DEMOCRAT_PARTY_PEJ = re.compile(r"\bdemocrat\s+party\b")
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
    """
    forms = sorted(morph_variants(w), key=len, reverse=True)
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

    parts = [r"\b" + r"\s+".join(frag(w) for w in p) + r"\b" for p in set(phrases)]
    # No re.IGNORECASE: patterns are built from lowercased tokens and only ever matched
    # against lowercased text (`low`/`win`), so the flag is pure overhead (~2.7x/scan).
    return re.compile("|".join(parts))


class _Lexicon:
    """A single lexicon: fast single-word set + optional multi-word regex."""

    def __init__(self, terms: List[str], fuzzy: bool = True) -> None:
        self.singles, self._phrases = _split_terms(terms, fuzzy=fuzzy)
        self._fuzzy = fuzzy
        self.phrase_re = _phrase_regex(self._phrases, fuzzy=fuzzy)
        self._build_overlap()

    def _build_overlap(self) -> None:
        """(Re)build per-weight overlap regexes from the CURRENT single-word set.

        A phrase whose content word is also a single-word term would be counted twice
        (once as the phrase, once by the single-word pass). For each such phrase we count
        how many of its content words overlap ``singles`` and subtract that many per phrase
        match in :meth:`count` — mirroring the cross-tier profanity de-dup invariant
        ("count each surface form once") WITHOUT dropping standalone single-word hits.
        Rebuildable because the profanity loader mutates ``singles`` after construction.
        """
        by_weight: Dict[int, List[Tuple[str, ...]]] = defaultdict(list)
        for p in self._phrases:
            overlap = sum(
                1 for w in p
                if w not in _FUNCTION and len(w) >= _MIN_INFLECT_LEN
                and ((morph_variants(w) if self._fuzzy else {w}) & self.singles)
            )
            if overlap:
                by_weight[overlap].append(p)
        self._overlap_res = [
            (wt, _phrase_regex(ps, fuzzy=self._fuzzy)) for wt, ps in by_weight.items()
        ]

    def count(self, tokens: Counter, text: str) -> int:
        # Intersect with the (usually small) token set instead of scanning all singles.
        n = sum(tokens[w] for w in self.singles.intersection(tokens))
        if self.phrase_re is not None and text:
            n += len(self.phrase_re.findall(text))
            # Subtract single-word occurrences already consumed by an overlapping phrase.
            for weight, rx in self._overlap_res:
                if rx is not None:
                    n -= weight * len(rx.findall(text))
        return n


def _load_profanity(fuzzy: bool = True) -> Dict[str, "_Lexicon"]:
    tiers: Dict[str, List[str]] = {"mild": [], "strong": [], "slurs": []}
    for line in _load_lines("profanity.txt"):
        term, _, tier = line.partition("\t")
        tier = tier.strip() or "strong"
        if tier in tiers:
            tiers[tier].append(term.strip())
    lex = {tier: _Lexicon(terms, fuzzy=fuzzy) for tier, terms in tiers.items()}
    # De-duplicate surface forms across tiers so each token is counted once, in its most
    # severe tier. Fuzzy expansion can make a mild term ("screw" -> "screwed") collide
    # with an explicit strong entry; without this the token is counted in both tiers and
    # ``profanity_hits`` (sum of tiers) double-counts it.
    lex["strong"].singles -= lex["slurs"].singles
    lex["mild"].singles -= lex["slurs"].singles | lex["strong"].singles
    # Overlap regexes were built against the pre-subtraction single-word sets; rebuild the
    # mutated tiers so phrase/single de-dup stays consistent with the final ``singles``.
    lex["strong"]._build_overlap()
    lex["mild"]._build_overlap()
    return lex


class Scorers:
    """Holds compiled lexicons; reused across all turns."""

    def __init__(self, use_sentiment: bool = False, fuzzy: bool = True) -> None:
        self.comity = _Lexicon(_load_lines("comity.txt"), fuzzy=fuzzy)
        self.hostility = _Lexicon(_load_lines("hostility.txt"), fuzzy=fuzzy)
        self.outgroup_idiom = _Lexicon(_load_lines("outgroup.txt"), fuzzy=fuzzy)
        self.profanity = _load_profanity(fuzzy=fuzzy)
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

    def _idiom_spans(self, text_lower: str) -> List[Tuple[int, int]]:
        if self.outgroup_idiom.phrase_re is None:
            return []
        return [m.span() for m in self.outgroup_idiom.phrase_re.finditer(text_lower)]

    def score_turn(self, text: str, party: str) -> Dict[str, float]:
        text = text or ""
        low = text.lower()
        # Single tokenization pass: build the token Counter, the word count, AND the
        # out-party name spans from one walk over the matches (no redundant re-scan).
        outtok = _OUTPARTY_TOKENS.get(party)
        tokens: Counter = Counter()
        outparty_spans: List[Tuple[int, int]] = []
        n_words = 0
        for m in _TOKEN_RE.finditer(low):
            g = m.group()
            tokens[g] += 1
            n_words += 1
            if outtok and g in outtok:
                outparty_spans.append(m.span())

        spans: List[Tuple[int, int]] = self._idiom_spans(low) + outparty_spans

        prof = {tier: lex.count(tokens, low) for tier, lex in self.profanity.items()}
        win = self._window_text(low, spans)
        win_tokens = Counter(_TOKEN_RE.findall(win)) if win else Counter()

        out: Dict[str, float] = {
            "n_words": n_words,
            "comity_hits": self.comity.count(tokens, low),
            "hostility_hits": self.hostility.count(tokens, low),
            "profanity_mild": prof.get("mild", 0),
            "profanity_strong": prof.get("strong", 0),
            "profanity_slurs": prof.get("slurs", 0),
            "profanity_hits": sum(prof.values()),
            "outgroup_refs": len(spans),
            # Only run the pejorative regex when the token "democrat" is present.
            "democrat_party_pej": len(_DEMOCRAT_PARTY_PEJ.findall(low)) if "democrat" in tokens else 0,
            "directed_comity_hits": self.comity.count(win_tokens, win),
            "directed_hostility_hits": self.hostility.count(win_tokens, win),
        }
        if self._sid is not None:
            compound, neg_share, n_sentences = self._sentiment(text)
            out["sentiment"] = compound
            out["neg_share"] = neg_share
            out["n_sentences"] = n_sentences
        return out
