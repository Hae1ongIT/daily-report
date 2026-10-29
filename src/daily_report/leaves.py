"""연차·반차 등록과 취소. 본인과 관리자가 같은 규칙으로 쓴다."""

import sqlite3
from datetime import date, datetime, timedelta

from daily_report.db import ANNUAL, HALF, Store
from daily_report.errors import RuleError
from daily_report.fmt import month_day
from daily_report.workcal import WorkCalendar

KIND_LABEL = {ANNUAL: "연차", HALF: "반차"}


def register_leave(
    store: Store,
    cal: WorkCalendar,
    user_id: str,
    start: date,
    end: date | None,
    kind: str,
    now: datetime,
    edit_window: int,
) -> list[date]:
    end = end or start
    if end < start:
        raise RuleError("종료일이 시작일보다 빨라요.", field="end")
    single = start == end
    if kind == HALF and not single:
        raise RuleError("반차는 하루 단위로 등록해요.", field="end")
    if single and not cal.is_workday(start):
        raise RuleError("쉬는 날에는 연차·반차를 등록할 수 없어요.", field="start")

    days = [day for day in _dates(start, end) if cal.is_workday(day)]
    if not days:
        raise RuleError("기간 안에 근무일이 없어요.", field="start")
    _check_window(cal, days, now, edit_window)

    with store.lock:  # 겹침 확인과 저장 사이에 다른 등록이 끼지 않게 한다
        taken = [(day, store.get_leave(user_id, day)) for day in days]
        taken = [(day, k) for day, k in taken if k]
        if taken:
            listed = ", ".join(f"{month_day(day)}은 이미 {KIND_LABEL[k]}" for day, k in taken)
            raise RuleError(f"{listed}로 등록돼 있어요. 바꾸려면 먼저 취소하세요.", field="start")
        try:
            store.add_leaves(user_id, days, kind)
        except sqlite3.IntegrityError as e:
            raise RuleError(
                "이미 등록된 날이 있어요. 앱 홈을 새로 열어 확인하세요.", "start"
            ) from e
    return days


def cancel_leave(
    store: Store,
    cal: WorkCalendar,
    user_id: str,
    start: date,
    end: date | None,
    now: datetime,
    edit_window: int,
) -> list[date]:
    end = end or start
    if end < start:
        raise RuleError("종료일이 시작일보다 빨라요.", field="end")
    days = [day for day in _dates(start, end) if store.get_leave(user_id, day)]
    if not days:
        raise RuleError("취소할 연차·반차가 없어요.", field="start")
    _check_window(cal, days, now, edit_window)

    store.delete_leaves(user_id, days)
    return days


def _check_window(cal: WorkCalendar, days: list[date], now: datetime, edit_window: int) -> None:
    today = now.date()
    expired = [day for day in days if day < today and today > cal.edit_last_day(day, edit_window)]
    if expired:
        raise RuleError(
            f"{month_day(expired[0])}은 수정 가능 기간이 지나서 바꿀 수 없어요.", field="start"
        )


def _dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
