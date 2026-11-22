"""
What a "day" is for a person: the calendar day in their wearable's time zone (the phone reports it with
each day of watch activity), else the hub's own zone. The zone is kept in timezone.json next to the vault.
"""
import json
import logging
import os
from datetime import date, datetime, timedelta, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from uniscale.config import vault_db_path

logger = logging.getLogger(__name__)


def _file(db_path: Optional[str]) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path or vault_db_path())), "timezone.json")


def valid_zone(name: str) -> str:
    """The IANA name if it is one (e.g. America/Chicago), else ValueError."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Unknown time zone '{name}'") from e
    return name


def zone_name(db_path: Optional[str] = None) -> Optional[str]:
    """The wearable's zone as last reported, or None."""
    try:
        with open(_file(db_path), "r", encoding="utf-8") as f:
            return valid_zone(json.load(f)["timezone"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning(f"Ignoring unreadable timezone.json: {e}")
        return None


def zone(db_path: Optional[str] = None) -> tzinfo:
    """The wearable's zone, else the hub's."""
    name = zone_name(db_path)
    return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo


def set_zone(name: str, db_path: Optional[str] = None) -> None:
    """Records the wearable's zone (a no-op when it hasn't changed)."""
    if zone_name(db_path) == valid_zone(name):
        return
    path = _file(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"timezone": name}, f)
    os.replace(tmp, path)
    logger.info(f"Day boundaries now follow {name}")


def today(db_path: Optional[str] = None) -> date:
    return datetime.now(zone(db_path)).date()


def yesterday(db_path: Optional[str] = None) -> str:
    return (today(db_path) - timedelta(days=1)).isoformat()


def local_day(ts, tz: tzinfo) -> str:
    """The calendar day (YYYY-MM-DD) of an ISO timestamp in tz. A timestamp without an offset is taken
    as already local (imports write those), and a bare date is itself."""
    s = str(ts or "")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10]
    if len(s) <= 10:
        return s[:10]
    return dt.astimezone(tz).date().isoformat() if dt.tzinfo else s[:10]
