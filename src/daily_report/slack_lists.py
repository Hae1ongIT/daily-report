"""월별 관리자 리스트에 보고를 한 방향으로 기록한다(DB → 리스트)."""

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from slack_sdk.errors import SlackApiError

from daily_report.db import Report, Store
from daily_report.fmt import month_key

log = logging.getLogger(__name__)

Api = Callable[[str, dict[str, Any]], dict[str, Any]]

SCHEMA = [
    {"key": "content", "name": "업무 내용", "type": "text", "is_primary_column": True},
    {"key": "date", "name": "날짜", "type": "date"},
    {"key": "user", "name": "이름", "type": "user", "options": {"format": "single_entity"}},
    {"key": "submitted_at", "name": "제출 시각", "type": "text"},
    {"key": "updated_at", "name": "수정 시각", "type": "text"},
]

# 행이 이미 없을 때 Slack이 돌려주는 오류(이때만 새 행을 만든다). 정확한 코드는 실제
# 운영에서 확인한다. 그 밖의 오류(레이트리밋 등)는 그대로 올려 다음 점검에서 맞춘다.
ROW_GONE = {"item_not_found", "invalid_row_id", "row_not_found", "record_not_found"}
LIST_GONE = {"list_not_found", "file_not_found", "file_deleted"}


class ListError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class ListSync:
    def __init__(self, api: Api, store: Store, admin_channel_id: str):
        self._api = api
        self._store = store
        self._admin_channel = admin_channel_id
        # 같은 달 리스트를 두 스레드가 동시에 만들거나, 같은 보고 행을 겹쳐 쓰지 않게 한다
        self._lock = threading.RLock()
        self._workspace: tuple[str, str] | None = None

    def upsert(self, report_id: int) -> None:
        """보고를 그 달 리스트에 맞춘다. 락 안에서 DB의 최신 보고를 다시 읽으므로, 기록 순서가
        뒤바뀌어도 옛 내용이 최신을 덮지 않고, 이미 지운 보고는 리스트에서도 지운다."""
        with self._lock:
            report = self._store.get_report_by_id(report_id)
            if report is None:
                self._remove_row(report_id)
                return
            month = _month(report.report_date)
            list_id = self.ensure_list(month)
            existing = self._store.get_list_item(report.id)
            try:
                if existing and existing[0] == list_id:
                    cells = [{"row_id": existing[1], **f} for f in self._fields(list_id, report)]
                    try:
                        self._call("slackLists.items.update", {"list_id": list_id, "cells": cells})
                        self._store.mark_list_item_synced(report.id, synced_state(report))
                        return
                    except ListError as e:
                        if e.code not in ROW_GONE:
                            raise
                        log.warning("리스트 행이 없어 새로 만듦: 보고 %s", report.id)
                        existing = None
                res = self._call(
                    "slackLists.items.create",
                    {"list_id": list_id, "initial_fields": self._fields(list_id, report)},
                )
            except ListError as e:
                if e.code in LIST_GONE:  # 리스트가 Slack에서 지워졌다. 다음에 새로 만든다.
                    log.warning("그 달 리스트가 없어 등록을 지움: %s", month)
                    self._store.forget_list(month)
                raise
            self._store.set_list_item(report.id, list_id, res["item"]["id"], synced_state(report))
            if existing:  # 달이 바뀐 이동: 새 행을 만든 뒤 옛 행을 지운다
                self._delete_quietly(*existing)

    def remove(self, report_id: int) -> None:
        """보고를 지운 뒤 부른다. DB에 보고가 아직 있으면 지우지 않는다."""
        with self._lock:
            if self._store.get_report_by_id(report_id) is None:
                self._remove_row(report_id)

    def _remove_row(self, report_id: int) -> None:
        existing = self._store.get_list_item(report_id)
        if not existing:
            return
        self._delete_item(*existing)
        self._store.delete_list_item(report_id)

    def _delete_quietly(self, list_id: str, item_id: str) -> None:
        try:
            self._delete_item(list_id, item_id)
        except ListError:
            log.warning("옛 리스트 행 삭제 실패(정합성 점검이 다시 지움): %s %s", list_id, item_id)
            self._store.add_stale_list_item(list_id, item_id)

    def _delete_item(self, list_id: str, item_id: str) -> None:
        """행을 지운다. 행이나 리스트가 이미 없으면 지운 것으로 친다."""
        try:
            self._call("slackLists.items.delete", {"list_id": list_id, "id": item_id})
        except ListError as e:
            if e.code not in ROW_GONE | LIST_GONE:
                raise

    def sync(self, start: date, end: date) -> dict[str, int]:
        """기간의 리스트 행을 DB 기준으로 다시 맞춘다(추가·수정·삭제).
        보고 하나가 실패해도 계속하고, 실패 건수를 돌려준다."""
        with self._lock:
            return self._sync(start, end)

    def _sync(self, start: date, end: date) -> dict[str, int]:
        reports = self._store.reports_between(start, end)
        listed: dict[str, list[dict[str, Any]]] = {}
        for month in _months(start, end):
            list_id = self._store.get_list_id(month)
            if list_id:
                listed[month] = self._items(list_id)
        in_range = [
            item
            for month, items in listed.items()
            for item in items
            if _in_range(_item_date(item, self._date_col(month)), start, end)
        ]
        if not reports and in_range:
            # DB 경로를 잘못 지정해 빈 DB로 켠 경우 리스트를 통째로 지우지 않게 막는다
            raise ListError("기간에 DB 보고가 없는데 리스트에는 행이 있어요. DB 경로를 확인하세요.")

        failed = 0
        for report in reports:
            try:
                self.upsert(report.id)
            except ListError:
                log.exception("동기화 중 보고 %s 기록 실패", report.id)
                failed += 1
        kept = set()
        for report in reports:
            item = self._store.get_list_item(report.id)
            if item:
                kept.add(item[1])

        deleted = 0
        for month, items in listed.items():
            list_id = self._store.get_list_id(month)
            date_col = self._date_col(month)
            for item in items:
                if not _in_range(_item_date(item, date_col), start, end) or item["id"] in kept:
                    continue
                try:
                    self._call("slackLists.items.delete", {"list_id": list_id, "id": item["id"]})
                    deleted += 1
                except ListError:
                    log.exception("동기화 중 행 %s 삭제 실패", item["id"])
                    failed += 1
        return {"upserted": len(reports) - failed, "deleted": deleted, "failed": failed}

    def reconcile(self, since: date) -> dict[str, int]:
        """DB만 보고 맞추는 정기 점검: 지운 보고의 행과 달을 옮기며 남은 옛 행은 지우고,
        리스트에 없거나 마지막 기록 뒤 바뀐 최근 보고는 다시 기록한다.
        (리스트 기록이 실패해 어긋난 것을 사람이 동기화하지 않아도 바로잡는다)"""
        upserted = removed = failed = 0
        with self._lock:
            for report_id in self._store.orphan_list_items():
                try:
                    self._remove_row(report_id)
                    removed += 1
                except ListError:
                    log.exception("정합성 점검: 지운 보고 %s의 행 삭제 실패", report_id)
                    failed += 1
            for list_id, item_id in self._store.stale_list_items():
                try:
                    self._delete_item(list_id, item_id)
                    self._store.remove_stale_list_item(list_id, item_id)
                    removed += 1
                except ListError:
                    log.exception("정합성 점검: 옛 행 %s 삭제 실패", item_id)
                    failed += 1
            # 리스트에 없는 보고, 그리고 수정·이동 기록이 실패해 내용이 어긋난 보고
            stale = self._store.reports_with_stale_list_item(since)
            for report_id in self._store.reports_without_list_item(since) + stale:
                try:
                    self.upsert(report_id)
                    upserted += 1
                except ListError:
                    log.exception("정합성 점검: 보고 %s 기록 실패", report_id)
                    failed += 1
        return {"upserted": upserted, "removed": removed, "failed": failed}

    def ensure_list(self, month: str) -> str:
        """그 달 리스트. 없으면 만들어 바로 기록하고, 관리자 채널 공유는 따로 끝낸다.
        공유가 실패해도 만든 리스트를 잃지 않고, 다음 호출에서 공유만 다시 한다.
        관리자 채널이 바뀌었으면 새 채널에 다시 공유한다."""
        with self._lock:
            list_id = self._store.get_list_id(month)
            if not list_id:
                res = self._call(
                    "slackLists.create", {"name": f"일일보고 {month}", "schema": SCHEMA}
                )
                list_id = res["list_id"]
                columns = {c["key"]: c["id"] for c in res["list_metadata"]["schema"]}
                self._store.register_list(month, list_id, columns)
            if self._store.list_shared_channel(month) != self._admin_channel:
                self._call(
                    "slackLists.access.set",
                    {
                        "list_id": list_id,
                        "access_level": "read",
                        "channel_ids": [self._admin_channel],
                    },
                )
                self._store.mark_list_shared(month, self._admin_channel)
            return list_id

    def permalink(self, day: date) -> str:
        """그 달 리스트 주소. 리스트가 없으면 새로 만들지 않고 빈 문자열."""
        list_id = self._store.get_list_id(_month(day))
        if not list_id:
            return ""
        if self._workspace is None:
            res = self._call("auth.test", {})
            self._workspace = (res["url"], res["team_id"])
        url, team_id = self._workspace
        return f"{url.rstrip('/')}/lists/{team_id}/{list_id}"

    def _date_col(self, month: str) -> str | None:
        list_id = self._store.get_list_id(month)
        return self._store.get_list_columns(list_id).get("date") if list_id else None

    def _fields(self, list_id: str, report: Report) -> list[dict[str, Any]]:
        col = self._store.get_list_columns(list_id)
        values = [
            ("content", {"rich_text": _rich(report.content)}),
            ("date", {"date": [report.report_date.isoformat()]}),
            ("user", {"user": [report.user_id]}),
            ("submitted_at", {"rich_text": _rich(_ts(report.submitted_at))}),
            ("updated_at", {"rich_text": _rich(_ts(report.updated_at))}),
        ]
        if report.updated_at is None:  # 빈 글자는 보내지 않는다(Slack이 빈 텍스트를 거절할 수 있음)
            values = [v for v in values if v[0] != "updated_at"]
        # 예전에 만든 달 리스트에 없는 열은 건너뛴다
        return [{"column_id": col[key], **value} for key, value in values if key in col]

    def _items(self, list_id: str) -> list[dict[str, Any]]:
        items, cursor = [], ""
        while True:
            payload = {"list_id": list_id, "limit": 100}
            if cursor:
                payload["cursor"] = cursor
            res = self._call("slackLists.items.list", payload)
            items += res.get("items", [])
            cursor = res.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return items

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            res = self._api(method, payload)
        except SlackApiError as e:  # slack_sdk는 ok:false면 예외를 던진다
            code = e.response.get("error")
            raise ListError(f"{method} 실패: {code}", code) from e
        except Exception as e:  # 네트워크 오류·타임아웃도 실패 건수로 세고 다음 건을 계속한다
            raise ListError(f"{method} 실패: {e}") from e
        if not res.get("ok"):
            raise ListError(f"{method} 실패: {res.get('error')}", res.get("error"))
        return res


def _rich(text: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "rich_text",
            "elements": [
                {"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}
            ],
        }
    ]


def synced_state(report: Report) -> str:
    """리스트 행에 기록한 보고 상태. 내용을 고치거나 날짜를 옮기면 수정 시각이 바뀐다."""
    at = report.updated_at or report.submitted_at
    return f"{report.report_date.isoformat()}|{at.isoformat()}"


def _ts(at: datetime | None) -> str:
    return at.strftime("%Y-%m-%d %H:%M") if at else ""


_month = month_key


def months_between(start: date, end: date) -> list[str]:
    months, (y, m) = [], (start.year, start.month)
    while (y, m) <= (end.year, end.month):
        months.append(month_key(date(y, m, 1)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


_months = months_between


def _in_range(day: date | None, start: date, end: date) -> bool:
    return day is not None and start <= day <= end


def _item_date(item: dict[str, Any], date_col: str | None) -> date | None:
    for f in item.get("fields", []):
        if f.get("column_id") == date_col and f.get("date"):
            return date.fromisoformat(f["date"][0])
    return None
