"""Per-speaker discourse counts, aggregated to a compact daily table.

The Congress-level metrics in :mod:`analysis.aggregate` cannot answer "who?", and
recomputing speaker rankings from the 6 GB turn corpus on every refresh is far too
slow to run unattended. This module produces a small, append-only daily table --
one row per ``(bioguide, date, chamber)`` -- that is cheap to store, cheap to
commit, and cheap to extend with a single new day of transcripts.

Attribution safeguards
----------------------
Naming individuals is a reputational claim, so the counts are deliberately
conservative:

* **Bioguide required.** Rows without a bioguide id are dropped rather than
  matched on surname, which collides ("Mr. SMITH") and drifts across Congresses.
* **Procedural turns excluded.** The Chair and presiding officers speak in the
  Record constantly but are not making a personal remark.
* **Quotations excluded.** The Record marks quoted passages with TeX-style
  ``\u0060\u0060 ... ''``. A member reading someone else's words is not swearing;
  roughly 5% of raw profanity hits in the 119th Congress fall inside quotations,
  which is more than enough to reorder a leaderboard. Quoted spans are masked
  before counting, and the masked hits are retained separately so the exclusion
  stays auditable.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from numbers import Integral
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import pandas as pd
import pyarrow.parquet as pq

from analysis.inputs import select_turn_files
from analysis.score.scorers import Scorers

LOG = logging.getLogger("analysis.speakers")

LANGUAGE_METRICS = {
    "profanity": {
        "hits": "profanity_hits",
        "rate": "profanity_per_100k",
        "label": "Profanity",
        "definition": "Unquoted matches from a narrow, curated profanity list.",
    },
    "hostility": {
        "hits": "hostility_hits",
        "rate": "hostility_per_100k",
        "label": "Personal hostility / disrespect",
        "definition": "Curated personal attack and disrespect terms.",
    },
    "misconduct": {
        "hits": "misconduct_hits",
        "rate": "misconduct_per_100k",
        "label": "Misconduct allegations",
        "definition": "Curated language alleging corruption, abuse, or other misconduct.",
    },
}

# The Record wraps quotations in TeX-style doubled backticks / apostrophes. The
# length cap stops a stray unmatched opener from swallowing a whole speech.
_QUOTE_RE = re.compile(r"``(.{0,4000}?)''", re.S)

_READ_COLS = [
    "turn_id", "source", "date", "congress", "chamber", "party", "state",
    "speaker_name", "bioguide", "word_count", "is_procedural", "text",
]

# Emitted per (bioguide, date, chamber) group.
_COUNT_KEYS = ("turns", "words", "profanity_hits", "profanity_quoted_hits",
               "hostility_hits", "misconduct_hits")
_TERM_COUNTS_COLUMN = "profanity_terms"

# Raw surface forms remain stored for auditability. Member summaries group only
# explicit morphological, spacing, and spelling variants into these families.
PROFANITY_TERM_FAMILIES = {
    "damn": ("damn", "damned", "dammit"),
    "goddamn": (
        "goddamn", "goddamned", "goddammit", "goddamnit",
        "god damn", "god damned", "god dammit", "god damnit",
    ),
    "hell": ("what the hell", "go to hell", "hell of a", "helluva"),
    "crap": ("crap", "crapped", "crapping", "crappy"),
    "bugger": ("bugger", "buggered", "buggers"),
    "frick": ("frickin", "fricking"),
    "frig": ("friggin", "frigging"),
    "bullshit": (
        "bullshit", "bull shit", "bullshitted", "bullshitter",
        "bullshitters", "bullshitting",
    ),
    "shit": ("shit", "shits", "shitting", "shitty"),
    "dipshit": ("dipshit", "dipshits"),
    "shit bag": ("shit bag", "shit bags", "shitbag", "shitbags"),
    "shithead": ("shithead", "shitheads"),
    "shithole": ("shithole", "shitholes"),
    "shit show": ("shit show", "shit shows", "shitshow", "shitshows"),
    "shitstorm": ("shitstorm", "shitstorms"),
    "fuck": ("fuck", "fucked", "fucking", "fucker", "fuckers", "fucks", "fuckin"),
    "fuck up": ("fuck up", "fucked up", "fucking up"),
    "fuckup": ("fuckup", "fuckups"),
    "clusterfuck": ("clusterfuck", "clusterfucks"),
    "motherfucker": (
        "motherfuck", "motherfucked", "motherfucks", "motherfucker",
        "motherfuckers", "motherfucking", "motherfuckin", "mother fucker",
        "mother fuckers", "mother fucking", "mother fuckin", "mothafucka",
        "mothafuckas", "mothafucker", "mothafuckers",
    ),
    "ass": ("ass", "asses"),
    "asshole": ("asshole", "assholes"),
    "assclown": ("assclown", "assclowns"),
    "asshat": ("asshat", "asshats"),
    "asswipe": ("asswipe", "asswipes"),
    "badass": ("badass", "badasses"),
    "fatass": ("fatass", "fatasses"),
    "kick ass": ("kick ass", "kicked ass", "kicking ass"),
    "smartass": ("smartass", "smartasses"),
    "wiseass": ("wiseass", "wiseasses"),
    "dumbass": ("dumbass", "dumbasses"),
    "jackass": ("jackass", "jackasses"),
    "bastard": ("bastard", "bastards"),
    "bitch": ("bitch", "bitches", "bitching", "bitchy"),
    "son of a bitch": (
        "son of a bitch", "son-of-a-bitch", "sons of bitches", "sons-of-bitches",
    ),
    "piss off": ("piss off", "pissed off", "pisses off", "pissing off"),
    "arsehole": ("arsehole", "arseholes"),
    "cunt": ("cunt", "cunts"),
    "dickhead": ("dickhead", "dickheads"),
    "douchebag": ("douchebag", "douchebags"),
    "cocksucker": (
        "cocksuck", "cocksucked", "cocksucker", "cocksuckers", "cocksucking",
        "cock sucker", "cock suckers", "cock sucking",
    ),
    "twat": ("twat", "twats"),
    "wanker": ("wanker", "wankers"),
}
_TERM_FAMILY_BY_FORM = {
    form: family
    for family, forms in PROFANITY_TERM_FAMILIES.items()
    for form in forms
}


def parse_profanity_terms(value) -> Counter:
    """Parse a stored profanity-term count map into a validated Counter."""
    if value is None:
        return Counter()
    if not isinstance(value, (str, dict)):
        try:
            if bool(pd.isna(value)):
                return Counter()
        except (TypeError, ValueError):
            pass
    raw = json.loads(value) if isinstance(value, str) else value
    if not isinstance(raw, dict):
        raise ValueError("profanity_terms must be a JSON object")
    counts = Counter()
    for term, count in raw.items():
        if not isinstance(term, str) or not term.strip():
            raise ValueError("profanity_terms keys must be non-empty strings")
        if isinstance(count, bool) or not isinstance(count, Integral) or count < 0:
            raise ValueError("profanity_terms counts must be non-negative integers")
        numeric = int(count)
        if numeric:
            counts[term] += numeric
    return counts


def serialize_profanity_terms(counts: Counter) -> str:
    """Serialize positive term counts deterministically for compact Parquet storage."""
    return json.dumps(
        {term: int(counts[term]) for term in sorted(counts) if counts[term] > 0},
        separators=(",", ":"),
    )


def combine_profanity_terms(values: Iterable) -> str:
    """Combine stored term-count maps and return deterministic JSON."""
    combined = Counter()
    for value in values:
        combined.update(parse_profanity_terms(value))
    return serialize_profanity_terms(combined)


def grouped_profanity_terms(value) -> Counter:
    """Group stored raw forms into explicit display families."""
    grouped = Counter()
    for term, count in parse_profanity_terms(value).items():
        grouped[_TERM_FAMILY_BY_FORM.get(term, term)] += count
    return grouped


def favorite_profanity_term(value) -> Tuple[str, int]:
    """Return the most frequent term family and count, breaking ties alphabetically."""
    counts = grouped_profanity_terms(value)
    if not counts:
        return "", 0
    term, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
    return term, int(count)


def incomplete_profanity_term_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Return rows whose stored surface-form total differs from profanity hits."""
    if frame.empty:
        return frame.copy()
    checked = _ensure_profanity_terms(frame).copy()
    checked["_profanity_term_hits"] = checked[_TERM_COUNTS_COLUMN].map(
        lambda value: sum(parse_profanity_terms(value).values())
    )
    return checked[
        checked["_profanity_term_hits"]
        != pd.to_numeric(checked["profanity_hits"], errors="raise")
    ]


def _ensure_profanity_terms(frame: pd.DataFrame) -> pd.DataFrame:
    if _TERM_COUNTS_COLUMN in frame:
        return frame
    result = frame.copy()
    result[_TERM_COUNTS_COLUMN] = "{}"
    return result


def mask_quotations(text: str) -> Tuple[str, str]:
    """Return ``(spoken, quoted)`` -- the text outside and inside quotation marks.

    Quoted regions are replaced by spaces rather than removed so that any
    character offsets and word boundaries either side stay intact.
    """
    if not text or "``" not in text:
        return text, ""
    quoted_parts: List[str] = []
    spans: List[Tuple[int, int]] = []
    for match in _QUOTE_RE.finditer(text):
        quoted_parts.append(match.group(1))
        spans.append(match.span())
    if not spans:
        return text, ""
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars), " ".join(quoted_parts)


def _iter_rows(path: Path, batch_size: int = 10_000) -> Iterator[dict]:
    handle = pq.ParquetFile(path)
    available = set(handle.schema_arrow.names)
    columns = [c for c in _READ_COLS if c in available]
    for batch in handle.iter_batches(batch_size=batch_size, columns=columns):
        data = batch.to_pydict()
        n = len(data[columns[0]])
        for i in range(n):
            yield {c: data[c][i] for c in columns}


def speaker_counts(
    files: Iterable[Path],
    scorers: Optional[Scorers] = None,
) -> pd.DataFrame:
    """Score turns and accumulate per ``(bioguide, date, chamber)`` counts."""
    scorers = scorers or Scorers()
    acc: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(
        lambda: {k: 0.0 for k in _COUNT_KEYS}
    )
    meta: Dict[str, dict] = {}
    row_congress: Dict[Tuple[str, str, str], int] = {}
    profanity_terms: Dict[Tuple[str, str, str], Counter] = defaultdict(Counter)
    seen_turn_ids: set[str] = set()

    for path in files:
        for row in _iter_rows(path):
            if row.get("is_procedural"):
                continue
            bioguide = (row.get("bioguide") or "").strip()
            if not bioguide:
                continue
            date = (row.get("date") or "")[:10]
            if len(date) != 10:
                continue
            turn_id = row.get("turn_id")
            if turn_id:
                if turn_id in seen_turn_ids:
                    continue
                seen_turn_ids.add(turn_id)

            text = row.get("text") or ""
            spoken, quoted = mask_quotations(text)
            party = row.get("party") or "other"
            scored = scorers.score_turn(spoken, party)
            quoted_scored = scorers.score_turn(quoted, party) if quoted else None
            turn_terms = scorers.profanity_term_counts(spoken)
            if sum(turn_terms.values()) != int(scored["profanity_hits"]):
                raise RuntimeError(
                    "profanity surface-form counts diverged from accepted hit count"
                )

            chamber = row.get("chamber") or "other"
            key = (bioguide, date, chamber)
            bucket = acc[key]
            bucket["turns"] += 1
            bucket["words"] += scored["n_words"]
            bucket["profanity_hits"] += scored["profanity_hits"]
            profanity_terms[key].update(turn_terms)
            bucket["hostility_hits"] += scored["hostility_hits"]
            bucket["misconduct_hits"] += scored["misconduct_hits"]
            if quoted_scored is not None:
                bucket["profanity_quoted_hits"] += quoted_scored["profanity_hits"]

            # Congress belongs to the *row*, not to the member. Taking it from the
            # member's latest metadata would relabel a long-serving member's whole
            # history as their most recent Congress, inflating that Congress's word
            # totals and deflating their rate.
            row_congress[key] = int(row.get("congress") or 0)

            # Party/state/name are member attributes, so the most recent value wins;
            # fall back to the previous value when a turn omits the name.
            current = meta.get(bioguide)
            if current is None or date >= current["last_seen"]:
                name = row.get("speaker_name") or (current or {}).get("speaker_name") or ""
                meta[bioguide] = {
                    "bioguide": bioguide,
                    "speaker_name": name,
                    "party": party,
                    "state": row.get("state") or (current or {}).get("state") or "",
                    "last_seen": date,
                }

    rows = []
    for (bioguide, date, chamber), counts in sorted(acc.items()):
        info = meta.get(bioguide, {})
        rows.append({
            "bioguide": bioguide,
            "date": date,
            "chamber": chamber,
            "speaker_name": info.get("speaker_name", ""),
            "party": info.get("party", "other"),
            "state": info.get("state", ""),
            "congress": row_congress.get((bioguide, date, chamber), 0),
            **{k: int(counts[k]) for k in _COUNT_KEYS},
            _TERM_COUNTS_COLUMN: serialize_profanity_terms(profanity_terms[
                (bioguide, date, chamber)
            ]),
        })
    return pd.DataFrame(rows, columns=[
        "bioguide", "date", "chamber", "speaker_name", "party", "state", "congress",
        *_COUNT_KEYS, _TERM_COUNTS_COLUMN,
    ])


def merge_daily(existing: Optional[pd.DataFrame], fresh: pd.DataFrame) -> pd.DataFrame:
    """Combine a stored daily table with newly computed rows.

    Fresh rows win for any ``(bioguide, date, chamber)`` they cover, so re-running a
    day repairs it instead of double counting.
    """
    fresh = _ensure_profanity_terms(fresh)
    if existing is None or existing.empty:
        combined = fresh
    elif fresh.empty:
        combined = _ensure_profanity_terms(existing)
    else:
        combined = pd.concat([
            _ensure_profanity_terms(existing),
            fresh,
        ], ignore_index=True)
        combined = combined.drop_duplicates(
            subset=["bioguide", "date", "chamber"], keep="last"
        )
    return combined.sort_values(["date", "bioguide", "chamber"]).reset_index(drop=True)


def replace_daily_window(
    existing: Optional[pd.DataFrame],
    fresh: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Replace every stored speaker row in a fully recomputed date window."""
    if existing is None or existing.empty:
        retained = existing
    else:
        dates = existing["date"].astype(str).str[:10]
        retained = existing[(dates < start) | (dates > end)]
    return merge_daily(retained, fresh)


def load_daily(path: Path) -> Optional[pd.DataFrame]:
    """Load the daily table from a partition directory (or a legacy single file).

    Storage is partitioned by Congress because the table is committed and rewritten
    by a scheduled job: rewriting one ~150 KB partition per run keeps git history
    small, where rewriting the whole multi-megabyte table daily would not.
    """
    if path.is_dir():
        parts = sorted(path.glob("congress_*.parquet"))
        if not parts:
            return None
        return _ensure_profanity_terms(
            pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        )
    if path.exists():
        return _ensure_profanity_terms(pd.read_parquet(path))
    return None


def save_daily(frame: pd.DataFrame, path: Path) -> List[Path]:
    """Write the daily table as one Parquet partition per Congress.

    Only partitions whose contents actually changed are rewritten, so an update that
    touches a single Congress produces a single-file diff.
    """
    frame = _ensure_profanity_terms(frame)
    path.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for congress, group in frame.groupby("congress", sort=True):
        part = path / f"congress_{int(congress):03d}.parquet"
        ordered = group.sort_values(["date", "bioguide", "chamber"]).reset_index(drop=True)
        if part.exists():
            try:
                if ordered.equals(pd.read_parquet(part)):
                    continue
            except Exception:  # noqa: BLE001 - unreadable partition: just rewrite it
                pass
        tmp = part.with_suffix(part.suffix + ".tmp")
        ordered.to_parquet(tmp, index=False)
        tmp.replace(part)
        written.append(part)
    return written


def leaderboard(
    daily: pd.DataFrame,
    min_words: int = 25_000,
    congress: Optional[int] = None,
    top: int = 25,
) -> pd.DataFrame:
    """Rank members by profanity rate per 100,000 spoken words.

    ``min_words`` guards against the small-sample artefact that otherwise dominates
    any rate ranking: a member with one profane word in 300 words of floor time
    would top a list of career orators. Members below the threshold are excluded
    rather than shown with an unstable rate.
    """
    frame = daily if congress is None else daily[daily["congress"] == congress]
    if frame.empty:
        return pd.DataFrame()
    frame = _ensure_profanity_terms(frame)
    grouped = frame.groupby("bioguide", as_index=False).agg(
        speaker_name=("speaker_name", "last"),
        party=("party", "last"),
        state=("state", "last"),
        chamber=("chamber", "last"),
        turns=("turns", "sum"),
        words=("words", "sum"),
        profanity_hits=("profanity_hits", "sum"),
        profanity_quoted_hits=("profanity_quoted_hits", "sum"),
        profanity_terms=(_TERM_COUNTS_COLUMN, combine_profanity_terms),
        first_date=("date", "min"),
        last_date=("date", "max"),
    )
    eligible = grouped[grouped["words"] >= min_words].copy()
    eligible["profanity_per_100k"] = (
        100_000 * eligible["profanity_hits"] / eligible["words"].where(eligible["words"] > 0)
    ).fillna(0.0)
    favorites = eligible[_TERM_COUNTS_COLUMN].map(favorite_profanity_term)
    eligible["favorite_profanity_term"] = favorites.map(lambda value: value[0])
    eligible["favorite_profanity_term_hits"] = favorites.map(lambda value: value[1])
    ranked = eligible.sort_values(
        ["profanity_per_100k", "profanity_hits"], ascending=False
    ).head(top).reset_index(drop=True)
    ranked.insert(0, "rank", range(1, len(ranked) + 1))
    return ranked


def timeseries(daily: pd.DataFrame, freq: str = "YE") -> pd.DataFrame:
    """Profanity rate per 100k words over time, for the whole chamber population."""
    if daily.empty:
        return pd.DataFrame()
    frame = daily.copy()
    frame["period"] = pd.to_datetime(frame["date"]).dt.to_period(
        "Y" if freq.startswith("Y") else "M"
    ).astype(str)
    grouped = frame.groupby(["period", "chamber"], as_index=False)[
        ["words", "profanity_hits", "turns"]
    ].sum()
    grouped["profanity_per_100k"] = (
        100_000 * grouped["profanity_hits"] / grouped["words"].where(grouped["words"] > 0)
    ).fillna(0.0)
    return grouped


def language_timeseries(
    daily: pd.DataFrame,
    congress: Optional[int] = None,
    *,
    by_chamber: bool = False,
) -> pd.DataFrame:
    """Return Democratic/Republican floor-language rates by month/year."""
    dimensions = ["period", "party", *(["chamber"] if by_chamber else [])]
    columns = [
        *dimensions, "words", "turns",
        *[metric["hits"] for metric in LANGUAGE_METRICS.values()],
        *[metric["rate"] for metric in LANGUAGE_METRICS.values()],
    ]
    if daily.empty:
        return pd.DataFrame(columns=columns)
    frame = daily if congress is None else daily[daily["congress"] == int(congress)]
    frame = frame[
        frame["chamber"].isin(["house", "senate"])
        & frame["party"].isin(["D", "R"])
    ].copy()
    if frame.empty:
        return pd.DataFrame(columns=columns)
    dates = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame[dates.notna()].copy()
    dates = dates[dates.notna()]
    frame["period"] = dates.dt.to_period("Y" if congress is None else "M").astype(str)
    hit_columns = [metric["hits"] for metric in LANGUAGE_METRICS.values()]
    grouped = frame.groupby(dimensions, as_index=False)[
        ["words", "turns", *hit_columns]
    ].sum()
    for metric in LANGUAGE_METRICS.values():
        grouped[metric["rate"]] = (
            100_000
            * grouped[metric["hits"]]
            / grouped["words"].where(grouped["words"] > 0)
        ).fillna(0.0)
    return grouped[columns].sort_values(dimensions).reset_index(drop=True)


def language_member_rates(
    daily: pd.DataFrame,
    congress: Optional[int] = None,
    *,
    min_words: int = 25_000,
    top: int = 8,
    chamber: Optional[str] = None,
) -> Dict[str, pd.DataFrame]:
    """Return deterministic top-member rate tables for each language measure."""
    frame = daily if congress is None else daily[daily["congress"] == int(congress)]
    frame = frame[frame["chamber"].isin(["house", "senate"])].copy()
    if chamber is not None:
        frame = frame[frame["chamber"] == chamber].copy()
    if frame.empty:
        return {key: pd.DataFrame() for key in LANGUAGE_METRICS}
    frame = frame.sort_values(["date", "bioguide", "chamber"])
    frame = _ensure_profanity_terms(frame)
    hit_columns = [metric["hits"] for metric in LANGUAGE_METRICS.values()]
    grouped = frame.groupby("bioguide", as_index=False).agg(
        speaker_name=("speaker_name", "last"),
        party=("party", "last"),
        state=("state", "last"),
        chamber=("chamber", "last"),
        words=("words", "sum"),
        turns=("turns", "sum"),
        active_days=("date", "nunique"),
        profanity_terms=(_TERM_COUNTS_COLUMN, combine_profanity_terms),
        **{column: (column, "sum") for column in hit_columns},
    )
    favorites = grouped[_TERM_COUNTS_COLUMN].map(favorite_profanity_term)
    grouped["favorite_profanity_term"] = favorites.map(lambda value: value[0])
    grouped["favorite_profanity_term_hits"] = favorites.map(lambda value: value[1])
    eligible = grouped[grouped["words"] >= int(min_words)].copy()
    rankings: Dict[str, pd.DataFrame] = {}
    for key, metric in LANGUAGE_METRICS.items():
        ranked_frame = eligible.copy()
        ranked_frame[metric["rate"]] = (
            100_000
            * ranked_frame[metric["hits"]]
            / ranked_frame["words"].where(ranked_frame["words"] > 0)
        ).fillna(0.0)
        ranked_frame = ranked_frame[ranked_frame[metric["hits"]] > 0]
        ordered = ranked_frame.sort_values(
            [metric["rate"], metric["hits"], "bioguide"],
            ascending=[False, False, True],
        ).head(top).reset_index(drop=True)
        ordered.insert(0, "rank", range(1, len(ordered) + 1))
        columns = [
            "rank", "bioguide", "speaker_name", "party", "state", "chamber",
            "words", "turns", "active_days", metric["hits"], metric["rate"],
        ]
        if key == "profanity":
            columns.extend([
                "favorite_profanity_term", "favorite_profanity_term_hits",
            ])
        rankings[key] = ordered[columns].copy()
    return rankings


def profanity_term_leaders(
    daily: pd.DataFrame,
    congress: Optional[int] = None,
    *,
    chamber: Optional[str] = None,
) -> List[dict]:
    """Return the member or tied members with the most uses of each profanity term."""
    frame = daily if congress is None else daily[daily["congress"] == int(congress)]
    frame = frame[frame["chamber"].isin(["house", "senate"])].copy()
    if chamber is not None:
        frame = frame[frame["chamber"] == chamber].copy()
    if frame.empty:
        return []
    frame = _ensure_profanity_terms(
        frame.sort_values(["date", "bioguide", "chamber"])
    )
    members: Dict[str, dict] = {}
    term_members: Dict[str, Counter] = defaultdict(Counter)
    term_totals: Counter = Counter()
    term_variants: Dict[str, set[str]] = defaultdict(set)
    for _, row in frame.iterrows():
        bioguide = str(row["bioguide"])
        members[bioguide] = {
            "bioguide": bioguide,
            "speaker_name": str(row["speaker_name"]),
            "party": str(row["party"]),
            "state": str(row["state"]),
            "chamber": str(row["chamber"]),
        }
        for raw_term, count in parse_profanity_terms(row[_TERM_COUNTS_COLUMN]).items():
            term = _TERM_FAMILY_BY_FORM.get(raw_term, raw_term)
            term_members[term][bioguide] += count
            term_totals[term] += count
            term_variants[term].add(raw_term)

    rows = []
    for term in sorted(term_members):
        leader_hits = max(term_members[term].values())
        leaders = [
            {**members[bioguide], "hits": int(leader_hits)}
            for bioguide, hits in sorted(term_members[term].items())
            if hits == leader_hits
        ]
        rows.append({
            "term": term,
            "leader_hits": int(leader_hits),
            "total_hits": int(term_totals[term]),
            "variants": sorted(term_variants[term]),
            "leaders": leaders,
        })
    return sorted(
        rows,
        key=lambda row: (-row["total_hits"], -row["leader_hits"], row["term"]),
    )


def build_daily(
    turns_dir: Path,
    out_path: Path,
    only_files: Optional[Iterable[Path]] = None,
) -> pd.DataFrame:
    """Compute speaker counts and merge them into the stored daily table."""
    files = list(only_files) if only_files is not None else select_turn_files(turns_dir)
    fresh = speaker_counts(files)
    merged = merge_daily(load_daily(out_path), fresh)
    save_daily(merged, out_path)
    LOG.info(
        "speaker daily table: +%d rows from %d file(s) -> %d rows total",
        len(fresh), len(files), len(merged),
    )
    return merged
