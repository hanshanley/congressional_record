"""Shared discovery helpers for normalized speaker-turn inputs."""

from __future__ import annotations

from pathlib import Path
from typing import List


def select_turn_files(turns_dir: Path) -> List[Path]:
    """Return all turn files in deterministic source-precedence order.

    A partial bulk file must never suppress fuller manifest coverage. GovInfo rows are
    unioned during scoring and deduplicated by ``turn_id``; bulk-first ordering means
    the bulk representation wins when both paths contain the same turn.
    """
    files = sorted(turns_dir.glob("*.parquet"))
    return sorted(
        files,
        key=lambda path: (
            0 if path.name.startswith("hein_") else
            1 if path.name.startswith("govinfo_bulk_") else
            2,
            path.name,
        ),
    )
