from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import CONFIG_PATH, DATA_DIR, EVENTS_PATH, AppConfig

_EVENTS_CACHE_MTIME: float | None = None
_EVENTS_CACHE_ROWS: list[dict[str, Any]] = []
_LAST_EVENTS_PRUNE_AT: datetime | None = None
EVENT_RETENTION_DAYS = 14
EVENT_MAX_ROWS = 50000
EVENT_PRUNE_INTERVAL_SECONDS = 3600


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    ensure_data_dir()
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def load_config() -> AppConfig:
    ensure_data_dir()
    if not CONFIG_PATH.exists():
        config = AppConfig()
        save_config(config)
        return config
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config = AppConfig.model_validate(payload)
    if config.model_dump() != payload:
        save_config(config)
    return config


def save_config(config: AppConfig) -> None:
    ensure_data_dir()
    atomic_write_text(CONFIG_PATH, json.dumps(config.model_dump(), indent=2, ensure_ascii=False))


def append_event(level: str, event: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_data_dir()
    maybe_prune_events()
    row = {
        "ts": utc_now_iso(),
        "level": level,
        "event": event,
        "detail": detail or {},
    }
    with EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def maybe_prune_events() -> None:
    global _LAST_EVENTS_PRUNE_AT
    now = datetime.now(UTC)
    if _LAST_EVENTS_PRUNE_AT and (now - _LAST_EVENTS_PRUNE_AT).total_seconds() < EVENT_PRUNE_INTERVAL_SECONDS:
        return
    _LAST_EVENTS_PRUNE_AT = now
    prune_events()


def prune_events(retention_days: int = EVENT_RETENTION_DAYS, max_rows: int = EVENT_MAX_ROWS) -> None:
    if not EVENTS_PATH.exists():
        return
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    kept: list[str] = []
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (json.JSONDecodeError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(json.dumps(row, ensure_ascii=False))
    if len(kept) > max_rows:
        kept = kept[-max_rows:]
    atomic_write_text(EVENTS_PATH, "\n".join(kept + [""]) if kept else "")
    global _EVENTS_CACHE_MTIME, _EVENTS_CACHE_ROWS
    _EVENTS_CACHE_MTIME = None
    _EVENTS_CACHE_ROWS = []


def recent_events(limit: int = 120) -> list[dict[str, Any]]:
    if not EVENTS_PATH.exists():
        return []
    lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    output = []
    for line in lines[-limit:]:
        try:
            output.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return output


def events_since(days: int = 7, event: str | None = None) -> list[dict[str, Any]]:
    if not EVENTS_PATH.exists():
        return []
    global _EVENTS_CACHE_MTIME, _EVENTS_CACHE_ROWS
    mtime = EVENTS_PATH.stat().st_mtime
    if _EVENTS_CACHE_MTIME != mtime:
        rows = []
        for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        _EVENTS_CACHE_MTIME = mtime
        _EVENTS_CACHE_ROWS = rows
    cutoff = datetime.now(UTC) - timedelta(days=days)
    output = []
    for row in _EVENTS_CACHE_ROWS:
        if event and row.get("event") != event:
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            output.append(row)
    return output


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
