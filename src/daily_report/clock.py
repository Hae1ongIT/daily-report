"""현재 시각을 주입하기 위한 시계. 테스트에서는 FixedClock으로 시각을 고정한다."""

from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def __init__(self, timezone: str):
        self._tz = ZoneInfo(timezone)

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FixedClock:
    def __init__(self, at: datetime):
        self.at = at

    def now(self) -> datetime:
        return self.at
