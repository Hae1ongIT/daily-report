"""앱 홈 날짜 목록: 수정 가능 기간이 남은 날 중 본인이 대상자였던 날의 상태."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from daily_report.db import ANNUAL, HALF, Store
from daily_report.targets import was_target
from daily_report.workcal import WorkCalendar


@dataclass(frozen=True)
class HomeRow:
    day: date
    status: str  # submitted | missing | half | annual
    last_day: date
    report_id: int | None = None
    preview: str = ""
    on_leave: bool = False


def home_rows(
    store: Store,
    cal: WorkCalendar,
    user_id: str,
    now: datetime,
    edit_window: int,
    targets: set[str],
) -> list[HomeRow]:
    """최근 날짜부터. 쉬는 날은 보고가 있을 때만 넣는다."""
    rows = []
    day = now.date()
    while True:
        # 수정 가능 기간의 끝은 날짜가 이를수록 이르므로, 한 번 지나면 그 전 날짜도 모두 지났다
        if day < now.date() and not cal.is_editable(day, now, edit_window):
            return rows
        row = _row(store, cal, user_id, day, edit_window, targets)
        if row:
            rows.append(row)
        day -= timedelta(days=1)


def _row(store, cal, user_id, day, edit_window, targets) -> HomeRow | None:
    report = store.get_report(user_id, day)
    leave = store.get_leave(user_id, day)
    last_day = cal.edit_last_day(day, edit_window)
    if not cal.is_workday(day):
        if not report:
            return None
    elif not was_target(store, cal, user_id, day, targets):
        return None
    if report:
        return HomeRow(day, "submitted", last_day, report.id, report.content, leave == ANNUAL)
    if leave == ANNUAL:
        return HomeRow(day, "annual", last_day)
    if leave == HALF:
        return HomeRow(day, "half", last_day)
    return HomeRow(day, "missing", last_day)


def upcoming_leaves(
    store: Store, cal: WorkCalendar, user_id: str, today: date, include_today: bool = False
) -> list[tuple[date, date, str]]:
    """내일 이후 연차·반차를 (첫날, 끝날, 종류)로. 사이에 쉬는 날만 낀 연차는 하나로 묶고,
    반차는 하루씩 둔다. 오늘은 앱 홈 날짜 목록에 나오므로 넣지 않는다(연차 창은 include_today).
    등록 뒤 휴무일이 되거나
    출근일 지정이 취소된 날의 연차는 쉬는 날이라 보여주지 않는다."""
    after = today - timedelta(days=1) if include_today else today
    return _group_leaves(cal, store.leaves_after(user_id, after))


def team_leaves(
    store: Store, cal: WorkCalendar, start: date, end: date
) -> dict[str, list[tuple[date, date, str]]]:
    """start~end의 연차·반차를 사람별 (첫날, 끝날, 종류) 목록으로.
    묶는 규칙은 upcoming_leaves와 같다."""
    by_user: dict[str, list[tuple[date, str]]] = {}
    for user_id, day, kind in store.leaves_between(start, end):
        by_user.setdefault(user_id, []).append((day, kind))
    grouped = {uid: _group_leaves(cal, items) for uid, items in by_user.items()}
    return {uid: groups for uid, groups in grouped.items() if groups}


def _group_leaves(cal: WorkCalendar, items: list[tuple[date, str]]) -> list[tuple[date, date, str]]:
    groups: list[tuple[date, date, str]] = []
    for day, kind in items:
        if not cal.is_workday(day):
            continue
        if (
            groups
            and kind == ANNUAL
            and groups[-1][2] == kind
            and cal.next_workday(groups[-1][1]) == day
        ):
            groups[-1] = (groups[-1][0], day, kind)
        else:
            groups.append((day, day, kind))
    return groups
