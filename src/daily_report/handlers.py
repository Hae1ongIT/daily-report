"""Slack 상호작용 처리. Bolt와 분리해서, 받은 값으로 규칙을 적용하고 Slack에 반영한다."""

import json
import logging
from datetime import date, timedelta
from typing import Any

from daily_report import views
from daily_report.clock import Clock
from daily_report.config import Config
from daily_report.db import ANNUAL, HALF, Store
from daily_report.directory import Directory
from daily_report.errors import RuleError
from daily_report.fmt import day_label
from daily_report.home import home_rows, team_leaves, upcoming_leaves
from daily_report.leaves import cancel_leave, register_leave
from daily_report.reports import default_date, delete_report, submit_report
from daily_report.targets import (
    add_off_day,
    current_targets,
    remove_off_day,
    remove_work_day,
    set_work_day,
)
from daily_report.workcal import OFF, WORK, WorkCalendar

log = logging.getLogger(__name__)

ADMIN_ONLY = "관리자만 할 수 있어요."
LEAVE_STATUS_DAYS = 28  # 연차·반차 현황: 오늘부터 4주


class Bot:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        slack: Any,
        lists: Any,
        clock: Clock,
        directory: Directory,
    ):
        self.cfg = cfg
        self.store = store
        self.slack = slack
        self.lists = lists
        self.clock = clock
        self.directory = directory

    # 공통

    def calendar(self) -> WorkCalendar:
        return WorkCalendar(self.store.overrides())

    def current_targets(self, fresh: bool = False) -> set[str]:
        """지금 보고 대상자. fresh면 사용자 목록을 캐시 대신 새로 읽는다(대상자 목록 저장 시)."""
        return current_targets(
            self.directory.members(fresh=fresh),
            self.store.exclusions(),
            self.cfg.test_target_users,
        )

    def notify(self, user_id: str, text: str) -> None:
        self.slack.chat_postMessage(channel=user_id, text=text)

    def open_view(self, user_id: str, trigger_id: str, view: dict) -> None:
        """창 열기가 실패하면(버튼 뒤 3초가 지나 trigger_id 만료 등) 사용자에게 알린다.
        알리지 않으면 버튼을 눌러도 아무 일이 없는 것처럼 보인다."""
        try:
            self.slack.views_open(trigger_id=trigger_id, view=view)
        except Exception:
            log.exception("창 열기 실패: %s", user_id)
            try:
                self.notify(user_id, "창을 열지 못했어요. 잠시 후 다시 눌러 주세요.")
            except Exception:
                log.exception("창 열기 실패 알림 실패: %s", user_id)

    # 앱 홈

    def home_opened(self, user_id: str) -> None:
        self.directory.knows(user_id)  # 목록에 없는 사람(방금 입사)이면 새로 읽는다
        targets = self.current_targets()
        is_target = user_id in targets
        rows = self.home_rows(user_id, targets) if is_target else []
        upcoming = (
            upcoming_leaves(self.store, self.calendar(), user_id, self.clock.now().date())
            if is_target
            else []
        )
        is_admin = self.directory.is_admin(user_id)
        off_days, work_days = self._upcoming_overrides() if is_admin else ([], [])
        home = views.app_home(is_target, is_admin, rows, upcoming, off_days, work_days)
        self.slack.views_publish(user_id=user_id, view=home)

    def home_rows(self, user_id: str, targets: set[str] | None = None) -> list[views.HomeRow]:
        targets = self.current_targets() if targets is None else targets
        return home_rows(
            self.store,
            self.calendar(),
            user_id,
            self.clock.now(),
            self.cfg.edit_window_workdays,
            targets,
        )

    # 보고

    def open_report(self, user_id: str, trigger_id: str, value: str) -> None:
        """value가 있으면 그 날짜(DM 버튼, 작성 버튼), 없으면 기본 날짜 규칙(앱 홈 버튼)."""
        if value:
            day = date.fromisoformat(value)
        else:
            day = default_date(
                self.store,
                self.calendar(),
                user_id,
                self.clock.now(),
                self.cfg.summary_time,
                self.cfg.edit_window_workdays,
                self.current_targets(),
            )
        existing = self.store.get_report(user_id, day)
        modal = views.report_modal(
            day, existing.content if existing else "", existing.id if existing else None
        )
        self.open_view(user_id, trigger_id, modal)

    def edit_report(self, user_id: str, trigger_id: str, value: str) -> None:
        report = self.store.get_report_by_id(int(value))
        if report is None:
            self.notify(user_id, "보고를 찾을 수 없어요. 앱 홈을 새로 열어 주세요.")
            return
        if report.user_id != user_id:
            self.notify(user_id, "본인 보고만 고칠 수 있어요.")
            return
        modal = views.report_modal(report.report_date, report.content, report.id)
        self.open_view(user_id, trigger_id, modal)

    def submit_report(self, user_id: str, view: dict, ack=None) -> dict[str, str] | None:
        return _respond(ack, *self._submit_report(user_id, view))

    def _submit_report(self, user_id: str, view: dict):
        meta = json.loads(view.get("private_metadata") or "{}")
        values = _values(view)
        try:
            result = submit_report(
                self.store,
                self.calendar(),
                user_id,
                date.fromisoformat(values["date"]),
                values.get("content") or "",
                self.clock.now(),
                self.cfg.edit_window_workdays,
                self.current_targets(),
                meta.get("existing_id"),
            )
        except RuleError as e:
            return {e.field: e.message}, None

        def after():
            # 리스트 기록은 동기화 중이면 오래 기다릴 수 있어서 앱 홈을 먼저 그린다
            self._refresh_home(user_id)
            if result.action == "moved":
                text = (
                    f"보고를 옮겼어요: {day_label(result.old_date)} "
                    f"→ {day_label(result.report.report_date)}"
                )
            else:
                text = f"{day_label(result.report.report_date)} 보고를 저장했어요."
            self._confirm(user_id, text)  # 리스트 기록은 오래 걸릴 수 있어서 먼저 알린다
            self._sync_list(lambda: self.lists.upsert(result.report.id))

        return None, after

    def delete_report(self, user_id: str, value: str) -> None:
        try:
            report = delete_report(
                self.store,
                self.calendar(),
                user_id,
                int(value),
                self.clock.now(),
                self.cfg.edit_window_workdays,
            )
        except RuleError as e:
            self.notify(user_id, e.message)
            return
        self._refresh_home(user_id)
        self._sync_list(lambda: self.lists.remove(report.id))

    def _confirm(self, user_id: str, text: str) -> None:
        """처리한 사람에게 결과를 알린다(SPEC). 실패해도 처리는 그대로 둔다."""
        try:
            self.notify(user_id, text)
        except Exception:
            log.exception("확인 DM 실패: %s", user_id)

    def _refresh_home(self, user_id: str) -> None:
        # 앱 홈을 못 그려도 뒤따르는 리스트 기록은 해야 한다
        try:
            self.home_opened(user_id)
        except Exception:
            log.exception("앱 홈 갱신 실패: %s", user_id)

    def _sync_list(self, action) -> None:
        # 리스트는 한 방향 기록용이라 실패해도 보고는 유지한다. 관리자가 동기화로 맞춘다.
        try:
            action()
        except Exception:
            log.exception("관리자 리스트 기록 실패")

    # 연차·반차

    def is_admin(self, user_id: str) -> bool:
        """관리 기능 실행 시 확인. 캐시가 아니라 관리자 채널 멤버를 새로 확인한다."""
        return self.directory.is_admin(user_id, fresh=True)

    def open_leave(self, user_id: str, trigger_id: str, register: bool, for_other: bool) -> None:
        if for_other and not self.is_admin(user_id):
            self.notify(user_id, ADMIN_ONLY)
            return
        upcoming = (
            []
            if for_other
            # 오늘 연차를 취소하려는 사람도 있어서 창에는 오늘을 넣는다
            else upcoming_leaves(
                self.store, self.calendar(), user_id, self.clock.now().date(), include_today=True
            )
        )
        self.open_view(user_id, trigger_id, views.leave_modal(register, for_other, upcoming))

    def submit_leave(self, user_id: str, view: dict, ack=None) -> dict[str, str] | None:
        return _respond(ack, *self._submit_leave(user_id, view))

    def _submit_leave(self, user_id: str, view: dict):
        meta = json.loads(view.get("private_metadata") or "{}")
        values = _values(view)
        target = user_id
        if meta.get("for_other"):
            if not self.is_admin(user_id):
                return {"user": ADMIN_ONLY}, None
            target = values["user"]
            # 등록만 보고 대상자로 제한한다. 취소는 제외 명단에 들어간 사람의
            # 남은 연차도 지울 수 있게 둔다.
            if meta.get("register") and target not in self.current_targets():
                return {"user": "보고 대상자가 아니에요. 제외 명단에 있으면 먼저 빼 주세요."}, None
        start = date.fromisoformat(values["start"])
        end = date.fromisoformat(values["end"]) if values.get("end") else None
        cal, now, window = self.calendar(), self.clock.now(), self.cfg.edit_window_workdays
        try:
            if meta.get("register"):
                kind = values.get("kind") or ANNUAL
                days = register_leave(self.store, cal, target, start, end, kind, now, window)
                what = f"{'반차' if kind == HALF else '연차'}를 등록했어요"
            else:
                days = cancel_leave(self.store, cal, target, start, end, now, window)
                what = "연차·반차를 취소했어요"
        except RuleError as e:
            return {e.field: e.message}, None

        def after():
            self._refresh_home(target)
            # 이름 조회(사용자 목록)는 응답을 보낸 뒤에 한다
            whose = "" if target == user_id else f"{self._name(target)}님의 "
            self._confirm(user_id, f"{whose}{what}: {_days_text(days)}")

        return None, after

    def _name(self, user_id: str) -> str:
        try:
            member = self.directory.by_id().get(user_id)
        except Exception:
            log.exception("사용자 목록 조회 실패")
            member = None
        return member.name if member else f"<@{user_id}>"  # 목록에 없으면(퇴사 등) 멘션으로

    # 관리 메뉴

    def open_admin(self, user_id: str, trigger_id: str, action_id: str) -> None:
        if not self.is_admin(user_id):
            self.notify(user_id, ADMIN_ONLY)
            return
        if action_id in (views.ADMIN_LEAVE, views.ADMIN_LEAVE_CANCEL):
            register = action_id == views.ADMIN_LEAVE
            self.open_view(user_id, trigger_id, views.leave_modal(register, True))
            return
        modal = {
            views.ADMIN_EXCLUSIONS: lambda: views.exclusions_modal(sorted(self.store.exclusions())),
            views.ADMIN_CALENDAR: self._calendar_modal,
            views.ADMIN_SYNC: views.sync_modal,
            views.ADMIN_LEAVE_STATUS: self._leave_status_modal,
        }[action_id]()
        self.open_view(user_id, trigger_id, modal)

    def _leave_status_modal(self) -> dict:
        start = self.clock.now().date()
        end = start + timedelta(days=LEAVE_STATUS_DAYS - 1)
        by_user = team_leaves(self.store, self.calendar(), start, end)
        # 지금 대상자만(제외 명단·퇴사자의 남은 연차는 빼고). 창을 열기 전이라 목록은 한 번만 본다
        targets, names = self.current_targets(), self.directory.by_id()
        people = sorted(
            (names[uid].name, groups) for uid, groups in by_user.items() if uid in targets
        )
        return views.leave_status_modal(start, end, people)

    def _calendar_modal(self) -> dict:
        return views.calendar_modal(*self._upcoming_overrides())

    def _upcoming_overrides(self) -> tuple[list[date], list[date]]:
        """오늘 이후 추가한 휴무일과 지정한 출근일(날짜순)."""
        today = self.clock.now().date()
        upcoming = sorted(item for item in self.store.overrides().items() if item[0] >= today)
        return (
            [d for d, kind in upcoming if kind == OFF],
            [d for d, kind in upcoming if kind == WORK],
        )

    def submit_exclusions(self, user_id: str, view: dict, ack=None) -> dict[str, str] | None:
        return _respond(ack, *self._submit_exclusions(user_id, view))

    def _submit_exclusions(self, user_id: str, view: dict):
        if not self.is_admin(user_id):
            return {"users": ADMIN_ONLY}, None
        selected = set(_values(view).get("users") or [])
        for uid in self.store.exclusions() - selected:
            self.store.remove_exclusion(uid)
        for uid in selected:
            self.store.add_exclusion(uid)
        return None, lambda: self._confirm(user_id, f"제외 명단을 저장했어요({len(selected)}명).")

    def submit_calendar(self, user_id: str, view: dict, ack=None) -> dict[str, str] | None:
        return _respond(ack, *self._submit_calendar(user_id, view))

    def _submit_calendar(self, user_id: str, view: dict):
        if not self.is_admin(user_id):
            return {"day": ADMIN_ONLY}, None
        values = _values(view)
        day, kind, now = date.fromisoformat(values["day"]), values.get("kind"), self.clock.now()
        try:
            if kind == "off":
                add_off_day(self.store, day, now, self.cfg.edit_window_workdays)
                what = "휴무일로 지정했어요"
            elif kind == "work":
                set_work_day(self.store, day, now)
                what = "출근일로 지정했어요"
            elif kind == "remove_off":
                remove_off_day(self.store, day, now)
                what = "추가한 휴무일을 취소했어요"
            elif kind == "remove_work":
                remove_work_day(self.store, day, now)
                what = "출근일 지정을 취소했어요"
            else:
                return {"kind": "변경 종류를 고르세요."}, None
        except RuleError as e:
            return {"day": e.message}, None

        def after():
            self._refresh_home(user_id)  # 관리 메뉴의 지정일 목록
            self._confirm(user_id, f"{what}: {day_label(day)}")

        return None, after

    def validate_sync(self, user_id: str, view: dict) -> dict[str, str] | None:
        if not self.is_admin(user_id):
            return {"start": ADMIN_ONLY}
        values = _values(view)
        if date.fromisoformat(values["end"]) < date.fromisoformat(values["start"]):
            return {"end": "종료일이 시작일보다 빨라요."}
        return None

    def run_sync(self, user_id: str, view: dict) -> None:
        if not self.is_admin(user_id):
            self.notify(user_id, ADMIN_ONLY)
            return
        values = _values(view)
        start, end = date.fromisoformat(values["start"]), date.fromisoformat(values["end"])
        try:
            result = self.lists.sync(start, end)
        except Exception as e:
            log.exception("리스트 동기화 실패")
            self.notify(user_id, f"리스트 동기화에 실패했어요. 잠시 후 다시 해 보세요. ({e})")
            return
        text = (
            f"{day_label(start)}~{day_label(end)} 리스트 동기화를 마쳤어요: "
            f"추가·수정 {result['upserted']}건, 삭제 {result['deleted']}건"
        )
        if result.get("failed"):
            text += f", 실패 {result['failed']}건(잠시 후 다시 해 보세요)"
        self.notify(user_id, text)

    def member_joined(self, channel_id: str, user_id: str) -> None:
        self.directory.member_joined(channel_id, user_id)

    def member_left(self, channel_id: str, user_id: str) -> None:
        self.directory.member_left(channel_id, user_id)


def _days_text(days: list[date]) -> str:
    """연차 날짜 안내. 기간이면 첫날~끝날과 실제 일수(쉬는 날 뺀 수)."""
    if len(days) == 1:
        return day_label(days[0])
    return f"{day_label(days[0])}~{day_label(days[-1])}, {len(days)}일"


def _respond(ack, errors, after):
    """Slack은 모달 제출에 3초 안의 응답을 요구한다. 응답을 먼저 보내고 나머지를 처리한다."""
    if ack and errors:
        ack(errors)
    elif ack:
        ack()
    if not errors and after:
        after()
    return errors


def _values(view: dict) -> dict[str, Any]:
    """모달 입력값을 블록 ID 기준으로 꺼낸다."""
    out: dict[str, Any] = {}
    for block_id, actions in view.get("state", {}).get("values", {}).items():
        for el in actions.values():
            kind = el.get("type")
            if kind == "datepicker":
                out[block_id] = el.get("selected_date")
            elif kind == "plain_text_input":
                out[block_id] = el.get("value")
            elif kind == "radio_buttons":
                out[block_id] = (el.get("selected_option") or {}).get("value")
            elif kind == "users_select":
                out[block_id] = el.get("selected_user")
            elif kind == "multi_users_select":
                out[block_id] = el.get("selected_users")
    return out
