"""Shard-level caching so aggregation rescopes to what actually changed.

Scoring the whole corpus takes ~21M turns through the lexicon scorers even when a
single day of new transcripts arrived. The aggregate is a *sum* over independent
groups, so it can be computed per shard, cached, and re-merged — leaving only the
changed shards to rescore.

A **shard** is the smallest set of turn files that must be scored together:

* GovInfo files are grouped by Congress. Both GovInfo ingesters mint the same
  ``crec:<granuleId>#<n>`` turn ids, so ``govinfo_119`` and ``govinfo_bulk_119``
  can contain the same turn and must be deduplicated against each other. A
  granule belongs to one package, which has one date, which falls in one
  Congress — so ids can never collide *across* Congresses, and per-Congress
  deduplication is equivalent to global deduplication.
* Every other file (e.g. ``hein_114``) is its own shard.

A cache entry is reused only when the shard's files are byte-for-byte the same
size and mtime *and* the scoring configuration fingerprint matches. The
fingerprint covers the lexicons, the scorer/registry/aggregate source, and the
scoring flags, so editing a lexicon or the scoring logic invalidates every entry
rather than silently mixing old and new definitions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger("analysis.incremental")

CACHE_VERSION = 2

# "<prefix>_<congress>.parquet" -- congress is the trailing numeric component.
_CONGRESS_RE = re.compile(r"^(?P<prefix>.+?)_(?P<congress>\d+)$")


@dataclass(frozen=True)
class Shard:
    """A group of turn files that must be scored together."""

    key: str
    files: Tuple[Path, ...]

    def fingerprint(self) -> str:
        """Identity of this shard's inputs: name, size and mtime of each file."""
        digest = hashlib.sha256()
        for path in self.files:
            stat = path.stat()
            digest.update(path.name.encode("utf-8"))
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
        return digest.hexdigest()


def shard_key(path: Path) -> str:
    """Return the shard a turn file belongs to.

    GovInfo files collapse to one shard per Congress so that duplicate turn ids
    are visible to a single scoring pass; anything else stands alone.
    """
    stem = path.stem
    match = _CONGRESS_RE.match(stem)
    if match and match.group("prefix").startswith("govinfo"):
        return f"govinfo:{int(match.group('congress')):03d}"
    return f"file:{stem}"


def plan_shards(files: Iterable[Path]) -> List[Shard]:
    """Group turn files into deterministically ordered shards."""
    grouped: Dict[str, List[Path]] = {}
    for path in files:
        grouped.setdefault(shard_key(path), []).append(path)
    return [
        Shard(key=key, files=tuple(sorted(grouped[key], key=lambda p: p.name)))
        for key in sorted(grouped)
    ]


def config_fingerprint(
    use_sentiment: bool,
    include_procedural: bool,
    extra_sources: Iterable[Path] = (),
) -> str:
    """Hash everything that can change a cached sum -- and nothing else.

    Only ``_score_shard``'s output is cached, so the fingerprint covers exactly the
    inputs to that: the lexicons, the scorer source, the accumulator key sets, the
    scoring flags, and the scoring function's own source.

    It deliberately does **not** hash whole modules. ``registry.py`` carries display
    titles and ``aggregate.py`` carries ``_finalize`` / ``_write_coverage``, which run
    *after* the cache on already-merged sums. Hashing those files meant renaming a
    chart label invalidated all 89 shards and forced a ~74 minute rescore that could
    not change a single number.
    """
    import inspect

    from analysis import aggregate as aggregate_module
    from analysis.score import registry as registry_module
    from analysis.score import scorers as scorers_module

    digest = hashlib.sha256()
    digest.update(f"v{CACHE_VERSION}".encode("utf-8"))
    digest.update(f"sentiment={int(use_sentiment)}".encode("utf-8"))
    digest.update(f"procedural={int(include_procedural)}".encode("utf-8"))

    lexicon_dir = Path(scorers_module.LEXDIR)
    for path in sorted(lexicon_dir.glob("*.txt")):
        digest.update(path.name.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())

    # Which score keys are accumulated, and under what names -- but not their titles.
    digest.update(repr(tuple(registry_module.SCORE_KEYS)).encode("utf-8"))
    digest.update(repr(tuple(aggregate_module._SUM_KEYS)).encode("utf-8"))

    # The scoring code itself: the scorer module, and the shard-scoring function whose
    # result is what actually gets cached.
    sources = [Path(scorers_module.__file__).resolve(), *extra_sources]
    for path in sources:
        if path.exists():
            digest.update(path.name.encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    try:
        digest.update(inspect.getsource(aggregate_module._score_shard).encode("utf-8"))
    except (OSError, TypeError):  # pragma: no cover - source always available in-tree
        digest.update(b"score_shard-source-unavailable")
    return digest.hexdigest()


def _encode_groups(groups: Dict[Tuple, Dict[str, float]]) -> List[dict]:
    return [
        {"key": list(key), "values": {k: v for k, v in sorted(values.items())}}
        for key, values in sorted(groups.items(), key=lambda item: [str(p) for p in item[0]])
    ]


def _decode_groups(payload: Iterable[dict]) -> Dict[Tuple, Dict[str, float]]:
    decoded: Dict[Tuple, Dict[str, float]] = {}
    for entry in payload:
        key = tuple(entry["key"])
        decoded[key] = dict(entry["values"])
    return decoded


class ShardCache:
    """Read/write cached per-shard sums keyed by shard and config fingerprint."""

    def __init__(self, path: Path, config_fp: str) -> None:
        self.path = path
        self.config_fp = config_fp
        self._entries: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("ignoring unreadable shard cache %s: %s", self.path, exc)
            return
        if payload.get("cache_version") != CACHE_VERSION:
            LOG.info("shard cache version changed; ignoring previous entries")
            return
        if payload.get("config_fingerprint") != self.config_fp:
            LOG.info("scoring configuration changed; every shard will be rescored")
            return
        entries = payload.get("shards")
        if isinstance(entries, dict):
            self._entries = entries

    def get(self, shard: Shard) -> Optional[Tuple[Dict[Tuple, Dict[str, float]], Dict[Tuple, Dict[str, float]]]]:
        """Return cached ``(acc, coverage)`` for ``shard``, or None if unusable."""
        entry = self._entries.get(shard.key)
        if not entry:
            return None
        try:
            if entry["fingerprint"] != shard.fingerprint():
                return None
            return _decode_groups(entry["acc"]), _decode_groups(entry["coverage"])
        except (KeyError, TypeError, OSError):
            return None

    def put(
        self,
        shard: Shard,
        acc: Dict[Tuple, Dict[str, float]],
        coverage: Dict[Tuple, Dict[str, float]],
    ) -> None:
        self._entries[shard.key] = {
            "fingerprint": shard.fingerprint(),
            "files": [p.name for p in shard.files],
            "acc": _encode_groups(acc),
            "coverage": _encode_groups(coverage),
        }

    def prune(self, live_keys: Iterable[str]) -> int:
        """Drop entries for shards that no longer exist; return how many."""
        live = set(live_keys)
        stale = [key for key in self._entries if key not in live]
        for key in stale:
            del self._entries[key]
        return len(stale)

    def save(self) -> None:
        payload = {
            "cache_version": CACHE_VERSION,
            "config_fingerprint": self.config_fp,
            "shards": self._entries,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        tmp.replace(self.path)
