from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import ScheduleConfig


class ScheduleGate:
    def __init__(self) -> None:
        self._fired_keys: set[str] = set()

    def due(self, config: ScheduleConfig, now: datetime | None = None, namespace: str = "") -> tuple[bool, str]:
        if not config.enabled:
            return False, ""
        zone = ZoneInfo(config.timezone)
        current = (now or datetime.now(zone)).astimezone(zone)
        key = ""

        if config.mode == "hourly":
            if current.minute != config.minute:
                return False, ""
            key = current.strftime("%Y-%m-%d-%H")
        elif config.mode == "daily":
            time_text = config.times[0] if config.times else "08:00"
            if current.strftime("%H:%M") != time_text:
                return False, ""
            key = current.strftime("%Y-%m-%d") + "-" + time_text
        else:
            time_text = current.strftime("%H:%M")
            if time_text not in config.times:
                return False, ""
            key = current.strftime("%Y-%m-%d") + "-" + time_text

        if namespace:
            key = f"{namespace}:{key}"
        if key in self._fired_keys:
            return False, ""
        self._fired_keys.add(key)
        if len(self._fired_keys) > 500:
            self._fired_keys = set(sorted(self._fired_keys)[-240:])
        return True, key
