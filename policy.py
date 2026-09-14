"""Notification windows.

Two rules, in order:
  1. Downtime is absolute. Nothing reaches the phone inside it.
  2. Each track has its own window on top of that.

A notification that comes due inside a closed window is never dropped — it is
held until the window next opens. `next_open` is what the workers schedule
against.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).with_name("config.yaml")
_cache: tuple[float, dict] | None = None


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Re-read config.yaml when it changes on disk, so edits land within a tick."""
    global _cache
    mtime = path.stat().st_mtime
    if _cache is None or _cache[0] != mtime:
        _cache = (mtime, yaml.safe_load(path.read_text()))
    return _cache[1]


def _parse_hm(s: str) -> int:
    """'18:00' -> minutes since midnight. '24:00' is end-of-day (1440)."""
    h, m = s.split(":")
    total = int(h) * 60 + int(m)
    if not 0 <= total <= 1440:
        raise ValueError(f"time out of range: {s}")
    return total


def _window_minutes(win: dict | None) -> tuple[int, int] | None:
    if win is None:
        return None
    return _parse_hm(win["start"]), _parse_hm(win["end"])


def _in_window(minute: int, window: tuple[int, int]) -> bool:
    """Half-open [start, end). Handles windows that wrap past midnight."""
    start, end = window
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def _effective_windows(track: str, when: datetime, cfg: dict) -> list[tuple[int, int]] | None:
    """The track window, or None if this track never notifies."""
    notify = cfg["notify"]
    tracks = dict(notify.get("tracks") or {})

    if when.weekday() >= 5:
        tracks.update(notify.get("weekend_override") or {})

    if track not in tracks:
        raise KeyError(f"unknown track {track!r}")
    track_win = _window_minutes(tracks[track])
    if track_win is None:
        return None
    return [track_win]


def can_notify(track: str, when: datetime, cfg: dict | None = None) -> bool:
    """May the brain push a `track` notification at `when`?"""
    cfg = cfg or load_config()
    windows = _effective_windows(track, when, cfg)
    if windows is None:
        return False

    minute = when.hour * 60 + when.minute
    downtime = _window_minutes(cfg["notify"]["downtime"])
    if downtime and _in_window(minute, downtime):
        return False
    return any(_in_window(minute, w) for w in windows)


def next_open(track: str, when: datetime, cfg: dict | None = None) -> datetime | None:
    """When this track can next be notified, at or after `when`.

    None means never (a track with no window). Resolution is one minute, and
    it looks eight days ahead so a weekend override can't cause a false never.
    """
    cfg = cfg or load_config()
    cursor = when.replace(second=0, microsecond=0)
    limit = cursor + timedelta(days=8)
    while cursor <= limit:
        if can_notify(track, cursor, cfg):
            return cursor
        cursor += timedelta(minutes=1)
    return None


def held_until(track: str, due: datetime, cfg: dict | None = None) -> tuple[bool, datetime | None]:
    """(delivers_now, when_it_will_deliver) for something coming due at `due`."""
    if can_notify(track, due, cfg):
        return True, due
    return False, next_open(track, due, cfg)
