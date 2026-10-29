"""보고 대상자 판단과 날짜별 대상자 목록, 휴무일·출근일 지정."""

from collections.abc import Callable, Iterable
from datetime import date, datetime

from daily_report.db import Store
from daily_report.errors import RuleError
from daily_report.members import Member
from daily_report.workcal import OFF, WORK, WorkCalendar


def current_targets(
    members: Iterable[Member], exclusions: set[str], test_users: tuple[str, ...]
) -> set[str]:
    """지금 보고 대상자. 시험 모드면 시험 대상자 안에서만 고른다(나머지 규칙은 그대로)."""
    targets = {
        mem.id
        for mem in members
        if not (mem.is_bot or mem.is_guest or mem.deleted) and mem.id not in exclusions
    }
    if test_users:
        targets &= set(test_users)
    return targets


class NoTargetsError(RuntimeError):
    pass


def ensure_targets(store: Store, day: date, current: Callable[[], set[str]]) -> set[str]:
    """그날 대상자 목록. 없으면 지금 대상자로 만들어 저장한다(17:30, 또는 대체 생성).
    지금 대상자가 한 명도 없으면(시험 대상자 ID 오타 등) 저장하지 않고 오류를 내서,
    고친 뒤 다음 실행에서 다시 만들게 한다. 한 번 저장한 목록은 고칠 방법이 없기 때문이다."""
    saved = store.get_targets(day)
    if saved is not None:
        return saved
    # 사용자 목록을 새로 읽는 동안 DB 락을 잡지 않는다(그동안 제출·앱 홈이 멈추지 않게)
    targets = current()
    if not targets:
        raise NoTargetsError(f"{day} 보고 대상자가 한 명도 없어요. 설정을 확인하세요.")
    with store.lock:
        saved = store.get_targets(day)  # 그사이 다른 작업이 먼저 저장했으면 그 목록을 쓴다
        if saved is not None:
            return saved
        store.save_targets(day, targets)
        return targets


def was_target(store: Store, cal: WorkCalendar, user_id: str, day: date, current: set[str]) -> bool:
    """그날 보고를 다룰 수 있는지. 쉬는 날이나 목록이 아직 없는 날은 지금 대상자인지로 본다.
    봇을 처음 켠 날 이전은 대상자 목록이 생길 수 없었던 날이라 대상이 아니었던 날로 본다."""
    installed = store.first_job_day("installed")
    if installed and day < installed:
        return False
    if cal.is_workday(day):
        saved = store.get_targets(day)
        if saved is not None:
            return user_id in saved
    return user_id in current


def add_off_day(store: Store, day: date, now: datetime, edit_window: int) -> None:
    """휴무일 추가. 미래와 오늘, 수정 가능 기간 안의 지난 날짜에 할 수 있다."""
    today = now.date()
    if day < today:
        cal = WorkCalendar(store.overrides())
        if today > cal.edit_last_day(day, edit_window):
            raise RuleError("수정 가능 기간이 지난 날짜는 휴무일로 바꿀 수 없어요.")
    store.set_override(day, OFF)
    store.delete_targets(day)


def set_work_day(store: Store, day: date, now: datetime) -> None:
    """주말·공휴일·휴무일을 출근일로 지정. 내일 이후만 된다."""
    _require_future(day, now)
    store.set_override(day, WORK)


def remove_off_day(store: Store, day: date, now: datetime) -> None:
    """관리자가 추가한 휴무일 취소. 그날이 근무일로 돌아가면 내일 이후만 된다."""
    override = store.overrides().get(day)
    if override == WORK:
        raise RuleError('출근일로 지정한 날이에요. "출근일 지정 취소"를 고르세요.')
    if override != OFF:
        raise RuleError("관리자가 추가한 휴무일이 아니에요.")
    if WorkCalendar().is_workday(day) and day <= now.date():
        raise RuleError("추가한 휴무일 취소는 그날이 근무일로 돌아가므로 내일 이후 날짜만 돼요.")
    store.remove_override(day)


def remove_work_day(store: Store, day: date, now: datetime) -> None:
    """출근일 지정 취소. 지정과 같이 내일 이후만 된다(그날 요청·대상자 목록이 생기기 전)."""
    if store.overrides().get(day) != WORK:
        raise RuleError("출근일로 지정한 날이 아니에요.")
    if day <= now.date():
        raise RuleError("출근일 지정 취소는 내일 이후 날짜만 할 수 있어요.")
    store.remove_override(day)


def _require_future(day: date, now: datetime) -> None:
    if day <= now.date():
        raise RuleError("출근일로 바꾸는 것은 내일 이후 날짜만 할 수 있어요.")
