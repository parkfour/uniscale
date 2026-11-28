"""
The watch's heart rate through the day (about one sample a minute), for the Watch detail's chart. The phone
sends the last day of it on every sync, so it's kept apart from the vault: heart_rate.json next to the vault,
{epoch seconds: bpm}, the last KEEP_DAYS days only. Re-sent samples simply overwrite themselves.
"""
import json
import logging
import os
import time
from typing import Dict, List, Optional, Sequence

from uniscale.config import read_json, vault_db_path, vault_locked, write_atomic

logger = logging.getLogger(__name__)

KEEP_DAYS = 7


def _file(db_path: Optional[str]) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path or vault_db_path())), "heart_rate.json")


def _load(db_path: Optional[str]) -> Dict[int, int]:
    data = read_json(_file(db_path), {})
    return {int(t): int(bpm) for t, bpm in data.items()} if isinstance(data, dict) else {}


@vault_locked
def add_samples(samples: Sequence[Sequence[int]], db_path: Optional[str] = None, now: Optional[float] = None) -> int:
    """Merges [[epoch seconds, bpm], ...] in; returns how many samples are kept."""
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    data = _load(db_path)
    data.update({int(t): int(bpm) for t, bpm in samples})
    data = {t: bpm for t, bpm in data.items() if t >= cutoff}
    write_atomic(_file(db_path), json.dumps({str(t): bpm for t, bpm in sorted(data.items())}))
    return len(data)


def recent(hours: float = 2, db_path: Optional[str] = None) -> Dict[str, object]:
    """The `hours` before the newest sample: {"samples": [[epoch s, bpm], ...], "end": newest epoch or None}."""
    data = _load(db_path)
    if not data:
        return {"samples": [], "end": None}
    end = max(data)
    since = end - hours * 3600
    samples: List[List[int]] = [[t, bpm] for t, bpm in sorted(data.items()) if t >= since]
    return {"samples": samples, "end": end}
