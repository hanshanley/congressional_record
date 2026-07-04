"""Modular civility scorers.

Loads the lexicons once and scores a turn's text for:

* comity/deference phrase hits (positive)
* hostility/attack hits (negative)
* profanity hits by tier (mild/strong/slurs)
* out-group reference count (aisle idioms + opposing-party name, resolved to the
  speaker's party) and **directed** comity/hostility within a window around each
  out-group reference ("civility toward the other party")
* the "Democrat party" pejorative marker
* optional VADER sentiment compound

Raw counts are returned per turn; rates-per-1,000-words are computed at aggregation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LEXDIR = Path(__file__).parent / "lexicons"

# Opposing-party name references, keyed by the SPEAKER's normalized party.
_PARTY_REF = {
    "D": re.compile(r"\b(republican|republicans|g\.?o\.?p\.?)\b", re.IGNORECASE),
    "R": re.compile(r"\b(democrat|democrats|democratic)\b", re.IGNORECASE),
}
# Adjectival-vs-noun misuse ("Democrat party" instead of "Democratic party") is
# itself a partisanship/incivility marker.
_DEMOCRAT_PARTY_PEJ = re.compile(r"\bdemocrat\s+party\b", re.IGNORECASE)

_WORD_RE = re.compile(r"\S+")


def _load_lines(name: str) -> List[str]:
    path = LEXDIR / name
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _phrase_regex(terms: List[str]) -> Optional[re.Pattern]:
    """Compile an alternation of terms/phrases with word boundaries.

    Spaces in a term become ``\\s+`` so OCR whitespace variation still matches.
    Longer alternatives are tried first so phrases win over their sub-words.
    """
    if not terms:
        return None
    parts = []
    for t in sorted(set(terms), key=len, reverse=True):
        esc = r"\s+".join(re.escape(w) for w in t.split())
        parts.append(esc)
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)


def _load_profanity() -> Dict[str, re.Pattern]:
    tiers: Dict[str, List[str]] = {"mild": [], "strong": [], "slurs": []}
    for line in _load_lines("profanity.txt"):
        if "\t" in line:
            term, tier = line.split("\t", 1)
        else:
            term, tier = line, "strong"
        tier = tier.strip()
        if tier in tiers:
            tiers[tier].append(term.strip())
    return {tier: p for tier, terms in tiers.items() if (p := _phrase_regex(terms))}


class Scorers:
    """Holds compiled lexicon regexes; reused across all turns."""

    def __init__(self, use_sentiment: bool = False) -> None:
        self.comity = _phrase_regex(_load_lines("comity.txt"))
        self.hostility = _phrase_regex(_load_lines("hostility.txt"))
        self.outgroup_idiom = _phrase_regex(_load_lines("outgroup.txt"))
        self.profanity = _load_profanity()
        self._sid = None
        if use_sentiment:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

            self._sid = SentimentIntensityAnalyzer()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _count(pat: Optional[re.Pattern], text: str) -> int:
        return len(pat.findall(text)) if pat else 0

    def _outgroup_spans(self, text: str, party: str) -> List[Tuple[int, int]]:
        spans = [m.span() for m in self.outgroup_idiom.finditer(text)] if self.outgroup_idiom else []
        pref = _PARTY_REF.get(party)
        if pref is not None:
            spans += [m.span() for m in pref.finditer(text)]
        return spans

    def _window_words(self, text: str, spans: List[Tuple[int, int]], radius: int = 30) -> str:
        """Concatenate ±``radius`` word windows around each span (deduped by word index)."""
        if not spans:
            return ""
        words = [(m.start(), m.end(), m.group()) for m in _WORD_RE.finditer(text)]
        if not words:
            return ""
        keep = set()
        for s, e in spans:
            # nearest word index to the span midpoint
            mid = (s + e) // 2
            idx = min(range(len(words)), key=lambda i: abs(words[i][0] - mid))
            for j in range(max(0, idx - radius), min(len(words), idx + radius + 1)):
                keep.add(j)
        return " ".join(words[j][2] for j in sorted(keep))

    # -- public ------------------------------------------------------------
    def score_turn(self, text: str, party: str) -> Dict[str, float]:
        text = text or ""
        n_words = len(_WORD_RE.findall(text))
        prof = {tier: self._count(pat, text) for tier, pat in self.profanity.items()}
        spans = self._outgroup_spans(text, party)
        win = self._window_words(text, spans)

        out: Dict[str, float] = {
            "n_words": n_words,
            "comity_hits": self._count(self.comity, text),
            "hostility_hits": self._count(self.hostility, text),
            "profanity_mild": prof.get("mild", 0),
            "profanity_strong": prof.get("strong", 0),
            "profanity_slurs": prof.get("slurs", 0),
            "profanity_hits": sum(prof.values()),
            "outgroup_refs": len(spans),
            "democrat_party_pej": len(_DEMOCRAT_PARTY_PEJ.findall(text)),
            # Directed: civility/hostility expressed *near* an out-group reference.
            "directed_comity_hits": self._count(self.comity, win),
            "directed_hostility_hits": self._count(self.hostility, win),
        }
        if self._sid is not None:
            # VADER caps well on short text; on long speeches score is still informative.
            out["sentiment"] = self._sid.polarity_scores(text[:5000])["compound"]
        return out
