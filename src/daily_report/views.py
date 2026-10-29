"""Slack Block Kit 화면: 보고 모달, DM, 앱 홈, 연차·관리 모달, 요약 메시지."""

import json
from datetime import date, time
from typing import Any

from daily_report.fmt import day_label, escape, weekday
from daily_report.home import HomeRow
from daily_report.summary import LlmSummary, SummaryData

# action_id / callback_id
OPEN_REPORT = "open_report"
WRITE_REPORT = "write_report"
EDIT_REPORT = "edit_report"
DELETE_REPORT = "delete_report"
OPEN_LEAVE = "open_leave"
OPEN_LEAVE_CANCEL = "open_leave_cancel"
ADMIN_EXCLUSIONS = "admin_exclusions"
ADMIN_CALENDAR = "admin_calendar"
ADMIN_SYNC = "admin_sync"
ADMIN_LEAVE = "admin_leave"
ADMIN_LEAVE_CANCEL = "admin_leave_cancel"
ADMIN_LEAVE_STATUS = "admin_leave_status"
LEAVE_STATUS_VIEW = "leave_status"  # 보기만 하는 창이라 제출 핸들러가 없다
REPORT_SUBMIT = "report_submit"
LEAVE_SUBMIT = "leave_submit"
EXCLUSIONS_SUBMIT = "exclusions_submit"
CALENDAR_SUBMIT = "calendar_submit"
SYNC_SUBMIT = "sync_submit"

Block = dict[str, Any]


def _text(s: str) -> dict:
    return {"type": "plain_text", "text": s}


def _md(s: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": s}}


def _button(label: str, action_id: str, value: str = "", style: str | None = None) -> dict:
    b = {"type": "button", "text": _text(label), "action_id": action_id}
    if value:
        b["value"] = value
    if style:
        b["style"] = style
    return b


def _date_input(
    block_id: str, label: str, initial: date | None = None, optional=False, hint: str = ""
) -> Block:
    el = {"type": "datepicker", "action_id": block_id}
    if initial:
        el["initial_date"] = initial.isoformat()
    block = {
        "type": "input",
        "block_id": block_id,
        "label": _text(label),
        "element": el,
        "optional": optional,
    }
    if hint:
        block["hint"] = _text(hint)
    return block


def _modal(
    callback_id: str,
    title: str,
    blocks: list[Block],
    meta: dict | None = None,
    submit: str = "저장",
) -> dict:
    # 닫기 버튼을 "취소"라고 하면 연차 취소 창에서 어느 버튼이 취소인지 헷갈린다
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": _text(title),
        "submit": _text(submit),
        "close": _text("닫기"),
        "blocks": blocks,
        "private_metadata": json.dumps(meta or {}),
    }


# 보고 모달


def report_modal(day: date, content: str = "", existing_id: int | None = None) -> dict:
    content_el = {
        "type": "plain_text_input",
        "action_id": "content",
        "multiline": True,
        "max_length": 3000,
        "placeholder": _text("한 일을 적어 주세요(최대 3,000자)"),
    }
    if content:
        content_el["initial_value"] = content
    hint = "날짜를 바꾸면 이 보고가 그 날짜로 옮겨져요." if existing_id else ""
    blocks = [
        _date_input("date", "날짜", day, hint=hint),
        {
            "type": "input",
            "block_id": "content",
            "label": _text("업무 내용"),
            "element": content_el,
        },
    ]
    title = "일일보고 수정" if existing_id else "일일보고"
    return _modal(REPORT_SUBMIT, title, blocks, {"existing_id": existing_id})


# DM


def request_text(day: date) -> str:
    return f"{day_label(day)} 일일보고를 써 주세요."


def reminder_text(day: date, summary_time: time) -> str:
    when = f"{summary_time:%H:%M}"
    return f"{day_label(day)} 일일보고가 아직 없어요. 오늘 {when} 요약 전에 써 주세요."


def request_dm(day: date) -> list[Block]:
    return [
        _md(request_text(day)),
        {"type": "actions", "elements": [_button("보고 쓰기", OPEN_REPORT, day.isoformat())]},
    ]


def reminder_dm(day: date, summary_time: time) -> list[Block]:
    return [
        _md(reminder_text(day, summary_time)),
        {"type": "actions", "elements": [_button("보고 쓰기", OPEN_REPORT, day.isoformat())]},
    ]


# 앱 홈


UPCOMING_MAX = 10


def app_home(
    is_target: bool,
    is_admin: bool,
    rows: list[HomeRow],
    upcoming: list[tuple[date, date, str]] = (),
    off_days: list[date] = (),
    work_days: list[date] = (),
) -> dict:
    """upcoming은 내일 이후 연차·반차 (첫날, 끝날, 종류). off_days, work_days는 관리자에게
    보여줄 오늘 이후 지정한 휴무일·출근일."""
    # 맨 위 제목은 두지 않는다(탭에 봇 이름이 보이고, 관리 메뉴 제목과 크기가 같아 구분이 안 됨)
    blocks: list[Block] = []
    if is_target:
        if upcoming:  # 관리 메뉴처럼 정보를 버튼 위에 둔다
            blocks.append(_md(_upcoming_text(upcoming)))
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button("보고 쓰기", OPEN_REPORT, style="primary"),
                    _button("연차·반차 등록", OPEN_LEAVE),
                    _button("연차·반차 취소", OPEN_LEAVE_CANCEL),
                ],
            }
        )
        blocks.append({"type": "divider"})
        for row in rows:
            blocks.extend(_home_row(row))
    else:
        blocks.append(_md("보고 대상이 아니에요. 보고가 필요하면 관리자에게 문의하세요."))

    if is_admin:
        blocks += [
            {"type": "divider"},
            {"type": "header", "text": _text("관리 메뉴")},
            _overrides_text(off_days, work_days),
            {
                "type": "actions",
                "elements": [
                    _button("제외 명단", ADMIN_EXCLUSIONS),
                    _button("휴무일·출근일", ADMIN_CALENDAR),
                    _button("리스트 동기화", ADMIN_SYNC),
                    _button("연차·반차 현황", ADMIN_LEAVE_STATUS),
                    _button("다른 사람 연차·반차 등록", ADMIN_LEAVE),
                    _button("다른 사람 연차·반차 취소", ADMIN_LEAVE_CANCEL),
                ],
            },
        ]
    return {"type": "home", "blocks": blocks}


def _home_row(row: HomeRow) -> list[Block]:
    head = f"*{day_label(row.day)}*"
    deadline = f"{day_label(row.last_day)}까지 수정 가능"
    if row.status == "annual":
        return [_md(f"{head} · 연차 · {deadline}")]
    if row.status in ("missing", "half"):
        label = "반차(선택 제출)" if row.status == "half" else "미제출"
        return [
            _md(f"{head} · {label} · {deadline}"),
            {
                "type": "actions",
                "elements": [_button("작성", WRITE_REPORT, row.day.isoformat(), "primary")],
            },
        ]
    tag = " (연차)" if row.on_leave else ""
    return [
        _md(f"{head} · 제출{tag} · {deadline}"),
        # 보고 본문은 서식(*, _, 링크, 이모지)으로 해석되지 않게 plain_text로 한 줄만 보여준다
        {"type": "section", "text": {**_text(_preview(row.preview)), "emoji": False}},
        {
            "type": "actions",
            "elements": [
                _button("수정", EDIT_REPORT, str(row.report_id)),
                {
                    **_button("삭제", DELETE_REPORT, str(row.report_id), "danger"),
                    "confirm": {
                        "title": _text("보고 삭제"),
                        "text": _text(f"{day_label(row.day)} 보고를 지울까요?"),
                        "confirm": _text("삭제"),
                        "deny": _text("아니요"),
                    },
                },
            ],
        },
    ]


def _upcoming_text(upcoming: list[tuple[date, date, str]]) -> str:
    return f"예정된 연차·반차: {_leaves_text(upcoming) or '없음'}"


def _leaves_text(groups: list[tuple[date, date, str]]) -> str:
    """(첫날, 끝날, 종류) 목록을 "11/3(화)~11/5(목) 연차, 11/9(월) 반차"로. 많으면 "외 N건"."""
    parts = []
    for first, last, kind in groups[:UPCOMING_MAX]:
        span = day_label(first) if first == last else f"{day_label(first)}~{day_label(last)}"
        parts.append(f"{span} {'반차' if kind == 'half' else '연차'}")
    more = len(groups) - UPCOMING_MAX
    return ", ".join(parts) + (f" 외 {more}건" if more > 0 else "")


def _preview(content: str, limit: int = 80) -> str:
    one_line = " ".join(content.split()) or "(내용 없음)"
    return _cut(one_line, limit)


# 연차·관리 모달


def leave_modal(
    register: bool, for_other: bool, upcoming: list[tuple[date, date, str]] = ()
) -> dict:
    """upcoming은 본인 창 위에 보여줄 내일 이후 연차·반차 (첫날, 끝날, 종류)."""
    blocks: list[Block] = []
    if not for_other:
        blocks.append(_md(_upcoming_text(upcoming)))
    if for_other:
        blocks.append(
            {
                "type": "input",
                "block_id": "user",
                "label": _text("사람"),
                "element": {"type": "users_select", "action_id": "user"},
            }
        )
    if register:
        blocks.append(
            {
                "type": "input",
                "block_id": "kind",
                "label": _text("종류"),
                "element": {
                    "type": "radio_buttons",
                    "action_id": "kind",
                    "initial_option": _option("annual", "연차"),
                    "options": [_option("annual", "연차"), _option("half", "반차(하루)")],
                },
            }
        )
    blocks += [
        _date_input("start", "시작일"),
        _date_input(
            "end",
            "종료일 (하루면 비워 두기)",
            optional=True,
            hint="기간 중 주말·공휴일·휴무일은 빼고 처리해요.",
        ),
    ]
    title = "연차·반차 등록" if register else "연차·반차 취소"
    meta = {"register": register, "for_other": for_other}
    return _modal(LEAVE_SUBMIT, title, blocks, meta, submit="등록" if register else title)


# Slack 한도: 창 블록 100개, section 텍스트 3,000자
STATUS_SECTION_MAX = 2900
STATUS_BLOCKS_MAX = 95


def leave_status_modal(
    start: date, end: date, people: list[tuple[str, list[tuple[date, date, str]]]]
) -> dict:
    """people은 (이름, (첫날, 끝날, 종류) 목록), 보여줄 순서대로."""
    blocks: list[Block] = [_md(f"{day_label(start)}~{day_label(end)} 연차·반차")]
    if not people:
        blocks.append(_md("이 기간에 등록된 연차·반차가 없어요."))
    chunk: list[str] = []
    for i, (name, groups) in enumerate(people):
        line = f"*{escape(name)}*: {_leaves_text(groups)}"
        if chunk and len("\n".join([*chunk, line])) > STATUS_SECTION_MAX:
            blocks.append(_md("\n".join(chunk)))
            chunk = []
            if len(blocks) >= STATUS_BLOCKS_MAX:
                blocks.append(_md(f"외 {len(people) - i}명"))
                break
        chunk.append(line)
    else:
        if chunk:
            blocks.append(_md("\n".join(chunk)))
    return {
        "type": "modal",
        "callback_id": LEAVE_STATUS_VIEW,
        "title": _text("연차·반차 현황"),
        "close": _text("닫기"),
        "blocks": blocks,
    }


def exclusions_modal(current: list[str]) -> dict:
    el = {"type": "multi_users_select", "action_id": "users"}
    if current:  # 빈 배열은 보내지 않는다
        el["initial_users"] = current
    blocks = [
        {
            "type": "input",
            "block_id": "users",
            "label": _text("보고 대상에서 뺄 사람"),
            "element": el,
            "optional": True,
            "hint": _text(
                "여기서 고른 사람이 제외 명단 전체예요(비우면 모두 보고 대상). "
                "그날 대상자는 17:30에 정해져서, 이미 정해진 날의 대상자는 바뀌지 않아요."
            ),
        }
    ]
    return _modal(EXCLUSIONS_SUBMIT, "제외 명단", blocks)


OVERRIDES_MAX = 20


def calendar_modal(off_days: list[date] = (), work_days: list[date] = ()) -> dict:
    """off_days, work_days는 오늘 이후 지정한 휴무일·출근일(날짜순)."""
    current = _overrides_text(off_days, work_days)
    options = [
        _option("off", "휴무일 추가", "오늘과 수정 가능 기간 안의 지난 날짜도 돼요"),
        _option("work", "출근일 지정(주말·공휴일·휴무일)", "내일 이후 날짜만 돼요"),
        _option("remove_off", "추가한 휴무일 취소", "근무일로 돌아가는 날이면 내일 이후만 돼요"),
        _option("remove_work", "출근일 지정 취소", "내일 이후 날짜만 돼요"),
    ]
    blocks = [
        current,
        _date_input("day", "날짜"),
        {
            "type": "input",
            "block_id": "kind",
            "label": _text("변경 종류"),
            "element": {"type": "radio_buttons", "action_id": "kind", "options": options},
        },
    ]
    return _modal(CALENDAR_SUBMIT, "휴무일·출근일", blocks, submit="적용")


def _overrides_text(off_days: list[date], work_days: list[date]) -> Block:
    return _md(f"추가한 휴무일: {_days_list(off_days)}\n지정한 출근일: {_days_list(work_days)}")


def _days_list(days: list[date]) -> str:
    if not days:
        return "없음"
    more = len(days) - OVERRIDES_MAX
    tail = f" 외 {more}건" if more > 0 else ""
    return ", ".join(day_label(d) for d in days[:OVERRIDES_MAX]) + tail


def sync_modal() -> dict:
    notice = _md(
        "이 기간의 관리자 리스트를 DB 기준으로 다시 맞춰요. "
        "리스트에만 있는 행(직접 넣은 행 포함)은 지워져요."
    )
    blocks = [notice, _date_input("start", "시작일"), _date_input("end", "종료일")]
    return _modal(SYNC_SUBMIT, "리스트 동기화", blocks, submit="동기화")


def _option(value: str, label: str, description: str = "") -> dict:
    option = {"value": value, "text": _text(label)}
    if description:
        option["description"] = _text(description)
    return option


# 요약 메시지


# Slack 한도: section 텍스트 3,000자, 표는 100행·셀 글자 합계 10,000자
TEAM_MAX = 2500
STATUS_MAX = 2900
CELL_MAX = 300
TABLE_ROWS_MAX = 100
TABLE_CHARS_MAX = 9500


def summary_message(
    data: SummaryData, llm: LlmSummary | None, links: list[tuple[str, str]]
) -> tuple[list[Block], str]:
    """links는 요약 기간이 걸친 달마다 (라벨, 리스트 주소)."""
    period = day_label(data.days[0])
    if len(data.days) > 1:
        period += f" ~ {day_label(data.days[-1])}"
    title = f"일일보고 요약 {period}"
    # 알림 미리보기에 보이는 글(text)에는 제출 현황을 붙인다
    preview = f"{title} · 제출 {data.submitted}/{data.total}" if data.workday else title

    status = [f"*제출 {data.submitted}/{data.total}*"] if data.workday else []
    for label, names in [
        ("미제출", data.missing),
        ("연차", data.annual),
        ("반차(미제출)", data.half_missing),
        ("제출(연차)", data.submitted_on_leave),
    ]:
        if names:
            status.append(f"{label}: {', '.join(escape(n) for n in names)}")
    if data.rest_reports:
        days_by_name: dict[str, list[str]] = {}
        for name, day in data.rest_reports:
            days_by_name.setdefault(name, []).append(weekday(day))
        rest = ", ".join(f"{escape(n)}({'·'.join(w)})" for n, w in days_by_name.items())
        status.append(f"쉬는 날 보고: {rest}")

    blocks: list[Block] = [{"type": "header", "text": _text(title)}]
    if status:
        blocks.append(_md(_cut(" / ".join(status), STATUS_MAX)))
    if data.request_missed:
        blocks.append(
            _md(
                f":warning: {day_label(data.workday)} 보고 요청 DM을 다 보내지 못했어요"
                "(봇이 꺼져 있었거나 오류). 미제출이 많을 수 있어요."
            )
        )
    if llm is None:
        blocks.append(_md(":warning: LLM 요약 실패. 원본은 리스트에서 보세요."))
    elif llm.people:
        blocks.append(_md(f"*팀 요약*\n{_cut(escape(llm.team), TEAM_MAX)}"))
        blocks += _people_table(llm.people)
    if links:
        many = len(links) > 1
        texts = [f"<{url}|원본 보고 리스트{f'({label})' if many else ''}>" for label, url in links]
        blocks.append(_md(" · ".join(texts)))
    return blocks, preview


def _people_table(people: list[tuple[str, str]]) -> list[Block]:
    rows = [[_cell("이름"), _cell("요약")]]
    used = len("이름") + len("요약")
    for name, summary in people:
        row = [_cell(_cut(name, 40)), _cell(_cut(summary, CELL_MAX))]
        size = sum(len(c["text"]) for c in row)
        if len(rows) >= TABLE_ROWS_MAX or used + size > TABLE_CHARS_MAX:
            break
        rows.append(row)
        used += size
    blocks: list[Block] = [{"type": "table", "rows": rows}]
    left = len(people) - (len(rows) - 1)
    if left:
        blocks.append(_md(f"외 {left}명은 길이 제한으로 생략했어요. 원본은 리스트에서 보세요."))
    return blocks


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cell(text: str) -> dict:
    return {"type": "raw_text", "text": text}


def missed_summary_text(days: list[date]) -> str:
    labels = ", ".join(day_label(d) for d in days)
    return (
        f"{labels} 보고 요약을 놓쳤어요(봇이 꺼져 있었거나 오류). "
        "이 날짜의 요약은 다시 올라오지 않아요. 원본은 관리자 리스트에서 보세요."
    )


def test_mode_text(count: int) -> str:
    return f"시험 모드: {count}명 대상. 정식 운영하려면 .env의 TEST_TARGET_USERS를 비우세요."
