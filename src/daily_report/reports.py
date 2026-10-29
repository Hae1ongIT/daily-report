"""보고 작성·수정·옮기기·삭제 규칙과 앱 홈 버튼의 기본 날짜."""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time

from daily_report.db import ANNUAL, HALF, Report, Store
from daily_report.errors import RuleError
from daily_report.fmt import month_day
from daily_report.targets import was_target
from daily_report.workcal import WorkCalendar

EXISTS_MESSAGE = "이미 보고가 있어요. 앱 홈에서 그 날짜 보고를 수정하세요."


@dataclass(frozen=True)
class SubmitResult:
    report: Report
    action: str  # created | updated | moved
    old_date: date | None = None


def submit_report(
    store: Store,
    cal: WorkCalendar,
    user_id: str,
    day: date,
    content: str,
    now: datetime,
    edit_window: int,
    current_targets: set[str],
    existing_id: int | None = None,
) -> SubmitResult:
    """모달 제출. existing_id는 모달을 열 때 불러온 보고(수정 창)."""
    content = content.strip()
    if not content:
        raise RuleError("업무 내용을 입력하세요.", field="content")
    if day > now.date():
        raise RuleError("미래 날짜는 고를 수 없어요.")
    if not was_target(store, cal, user_id, day, current_targets):
        raise RuleError(
            "이 날짜는 보고 대상이 아니었어요(그날 보고 요청 때 대상자 목록에 없었음). "
            "보고가 필요하면 관리자에게 문의하세요."
        )
    _require_editable(cal, day, now, edit_window)

    # 확인과 저장 사이에 다른 제출이 끼지 않게 한 덩어리로 처리한다
    with store.lock:
        try:
            return _save(store, cal, user_id, day, content, now, edit_window, existing_id)
        except sqlite3.IntegrityError as e:  # 같은 날짜 보고가 동시에 들어온 경우
            raise RuleError(EXISTS_MESSAGE) from e


def _save(store, cal, user_id, day, content, now, edit_window, existing_id) -> SubmitResult:
    existing = _own_report(store, user_id, existing_id) if existing_id else None
    if existing and existing.report_date == day:
        store.update_report(existing.id, content, now)
        return SubmitResult(store.get_report_by_id(existing.id), "updated")

    if store.get_report(user_id, day):
        raise RuleError(EXISTS_MESSAGE)

    if existing:
        _require_editable(cal, existing.report_date, now, edit_window)
        store.move_report(existing.id, day, content, now)
        return SubmitResult(store.get_report_by_id(existing.id), "moved", existing.report_date)

    report_id = store.add_report(user_id, day, content, now)
    return SubmitResult(store.get_report_by_id(report_id), "created")


def delete_report(
    store: Store, cal: WorkCalendar, user_id: str, report_id: int, now: datetime, edit_window: int
) -> Report:
    report = _own_report(store, user_id, report_id)
    _require_editable(cal, report.report_date, now, edit_window)
    store.delete_report(report.id)
    return report


def default_date(
    store: Store,
    cal: WorkCalendar,
    user_id: str,
    now: datetime,
    summary_time: time,
    edit_window: int,
    current_targets: set[str],
) -> date:
    """앱 홈 보고 버튼의 기본 날짜(SPEC: 앱 홈 버튼의 기본값 규칙)."""
    today = now.date()
    # 오늘 목록(17:30)이 정해진 뒤 대상이 된 사람은 오늘 보고를 낼 수 없으므로 건너뛴다
    if (
        cal.is_workday(today)
        and now.time() >= summary_time
        and was_target(store, cal, user_id, today, current_targets)
    ):
        return today

    candidate = today
    while True:
        candidate = cal.prev_workday(candidate)
        if not cal.is_editable(candidate, now, edit_window):
            return today
        leave = store.get_leave(user_id, candidate)
        if leave == ANNUAL:
            continue
        if not was_target(store, cal, user_id, candidate, current_targets):
            continue
        report = store.get_report(user_id, candidate)
        if leave == HALF and report is None:
            continue
        return candidate if report is None else today


def _own_report(store: Store, user_id: str, report_id: int) -> Report:
    report = store.get_report_by_id(report_id)
    if report is None:
        raise RuleError("보고를 찾을 수 없어요. 앱 홈을 새로 열어 주세요.")
    if report.user_id != user_id:
        raise RuleError("본인 보고만 고칠 수 있어요.")
    return report


def _require_editable(cal: WorkCalendar, day: date, now: datetime, edit_window: int) -> None:
    if not cal.is_editable(day, now, edit_window):
        raise RuleError(f"{month_day(day)}은 수정 가능 기간이 지났어요.")
