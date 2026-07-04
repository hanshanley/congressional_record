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
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

LEXDIR = Path(__file__).parent / "lexicons"

# Opposing-party name references, keyed by the SPEAKER's normalized party.
_OUTPARTY_TOKENS = {
    "D": {"republican", "republicans", "gop"},
    "R": {"democrat", "democrats", "democratic"},
}
_DEMOCRAT_PARTY_PEJ = re.compile(r"\bdemocrat\s+party\b", re.IGNORECASE)
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


def _split_terms(terms: List[str]) -> Tuple[Set[str], List[Tuple[str, ...]]]:
    """Partition terms into a single-word set and multi-word token-tuple list."""
    singles: Set[str] = set()
    phrases: List[Tuple[str, ...]] = []
    for t in terms:
        toks = _TOKEN_RE.findall(t.lower())
        if len(toks) == 1:
            singles.add(toks[0])
        elif len(toks) > 1:
            phrases.append(tuple(toks))
    return singles, phrases


def _phrase_regex(phrases: List[Tuple[str, ...]]) -> Optional[re.Pattern]:
    if not phrases:
        return None
    parts = [r"\b" + r"\s+".join(re.escape(w) for w in p) + r"\b" for p in phrases]
    return re.compile("|".join(parts), re.IGNORECASE)


class _Lexicon:
    """A single lexicon: fast single-word set + optional multi-word regex."""

    def __init__(self, terms: List[str]) -> None:
        self.singles, phrases = _split_terms(terms)
        self.phrase_re = _phrase_regex(phrases)

    def count(self, tokens: Counter, text: str) -> int:
        n = sum(tokens[w] for w in self.singles if w in tokens)
        if self.phrase_re is not None and text:
            n += len(self.phrase_re.findall(text))
        return n


def _load_profanity() -> Dict[str, "_Lexicon"]:
    tiers: Dict[str, List[str]] = {"mild": [], "strong": [], "slurs": []}
    for line in _load_lines("profanity.txt"):
        term, _, tier = line.partition("\t")
        tier = tier.strip() or "strong"
        if tier in tiers:
            tiers[tier].append(term.strip())
    return {tier: _Lexicon(terms) for tier, terms in tiers.items()}


class Scorers:
    """Holds compiled lexicons; reused across all turns."""

    def __init__(self, use_sentiment: bool = False) -> None:
        self.comity = _Lexicon(_load_lines("comity.txt"))
        self.hostility = _Lexicon(_load_lines("hostility.txt"))
        self.outgroup_idiom = _Lexicon(_load_lines("outgroup.txt"))
        self.profanity = _load_profanity()
        self._sid = None
        if use_sentiment:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

            self._sid = SentimentIntensityAnalyzer()

    @staticmethod
    def _window_text(text: str, spans: List[Tuple[int, int]], radius: int = 200) -> str:
        """Concatenate ±``radius``-char windows around each span (merged intervals)."""
        if not spans:
            return ""
        ivs = sorted((max(0, s - radius), min(len(text), e + radius)) for s, e in spans)
        merged: List[List[int]] = [list(ivs[0])]
        for s, e in ivs[1:]:
            if s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        return " ".join(text[s:e] for s, e in merged)

    def _outgroup_spans(self, text_lower: str, party: str) -> List[Tuple[int, int]]:
        spans: List[Tuple[int, int]] = []
        if self.outgroup_idiom.phrase_re is not None:
            spans += [m.span() for m in self.outgroup_idiom.phrase_re.finditer(text_lower)]
        outtok = _OUTPARTY_TOKENS.get(party)
        if outtok:
            for m in _TOKEN_RE.finditer(text_lower):
                if m.group() in outtok:
                    spans.append(m.span())
        return spans

    def score_turn(self, text: str, party: str) -> Dict[str, float]:
        text = text or ""
        low = text.lower()
        tokens = Counter(_TOKEN_RE.findall(low))
        n_words = sum(tokens.values())

        prof = {tier: lex.count(tokens, low) for tier, lex in self.profanity.items()}
        spans = self._outgroup_spans(low, party)
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
            "democrat_party_pej": len(_DEMOCRAT_PARTY_PEJ.findall(low)),
            "directed_comity_hits": self.comity.count(win_tokens, win),
            "directed_hostility_hits": self.hostility.count(win_tokens, win),
        }
        if self._sid is not None:
            out["sentiment"] = self._sid.polarity_scores(text[:5000])["compound"]
        return out
