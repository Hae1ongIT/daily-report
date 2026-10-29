"""근무일 달력: 주말·한국 공휴일은 쉬고, 관리자 지정(휴무일·출근일)이 우선한다."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta

import holidays

OFF = "off"
WORK = "work"

# 연속된 쉬는 날을 무한히 찾지 않도록 막는 상한
_MAX_SCAN_DAYS = 366


class WorkCalendar:
    def __init__(self, overrides: Mapping[date, str] | None = None):
        self._overrides = dict(overrides or {})
        self._holidays = holidays.country_holidays("KR")

    def is_workday(self, day: date) -> bool:
        override = self._overrides.get(day)
        if override is not None:
            return override == WORK
        return day.weekday() < 5 and day not in self._holidays

    def prev_workday(self, day: date) -> date:
        return self._scan(day, -1)

    def next_workday(self, day: date) -> date:
        return self._scan(day, 1)

    def edit_last_day(self, report_date: date, workdays: int) -> date:
        """수정 가능 기간의 마지막 날: 보고 날짜 다음 근무일부터 세어 n번째 근무일."""
        day = report_date
        for _ in range(workdays):
            day = self.next_workday(day)
        return day

    def is_editable(self, report_date: date, now: datetime, workdays: int) -> bool:
        today = now.date()
        return report_date <= today <= self.edit_last_day(report_date, workdays)

    def summary_range(self, summary_day: date) -> list[date]:
        """summary_day 요약이 다루는 날짜: 직전 근무일과 그 뒤 쉬는 날들."""
        start = self.prev_workday(summary_day)
        return [start + timedelta(days=i) for i in range((summary_day - start).days)]

    def _scan(self, day: date, step: int) -> date:
        for i in range(1, _MAX_SCAN_DAYS + 1):
            candidate = day + timedelta(days=step * i)
            if self.is_workday(candidate):
                return candidate
        raise ValueError(f"{day} 전후 {_MAX_SCAN_DAYS}일 안에 근무일이 없습니다")
