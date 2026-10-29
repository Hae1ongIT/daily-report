"""일일 요약에 들어갈 제출 현황과 사람별 보고 모으기(LLM 호출 전 단계)."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date

from daily_report.db import ANNUAL, HALF, Store
from daily_report.members import Member
from daily_report.workcal import WorkCalendar


@dataclass(frozen=True)
class PersonReports:
    user_id: str
    name: str
    entries: list[tuple[date, str]]


@dataclass(frozen=True)
class LlmSummary:
    team: str
    people: list[tuple[str, str]]  # (이름, 사람별 요약)


@dataclass(frozen=True)
class SummaryData:
    workday: date | None  # 이미 요약한 근무일이면 None(쉬는 날 보고만 남은 경우)
    rest_days: list[date]
    days: list[date]
    submitted: int
    total: int
    missing: list[str]
    annual: list[str]
    half_missing: list[str]
    submitted_on_leave: list[str]
    rest_reports: list[tuple[str, date]]
    people: list[PersonReports] = field(default_factory=list)
    # 그날 보고 요청 DM을 다 보내지 못했다(봇이 꺼져 있었거나 오류). 미제출이 많은 이유를 알린다.
    request_missed: bool = False


def collect_summary(
    store: Store,
    cal: WorkCalendar,
    summary_day: date,
    members: Mapping[str, Member],
    already: Collection[date] = (),
) -> SummaryData:
    """summary_day에 올릴 요약 자료. 그날 대상자 목록은 미리 만들어져 있어야 한다.

    already는 이미 다른 요약에서 다룬 날짜다. 예를 들어 근무일을 요약한 뒤 그 다음 날을
    휴무일로 바꾸면, 다음 요약은 그 근무일을 빼고 새로 생긴 쉬는 날 보고만 다룬다.
    """
    span = cal.summary_range(summary_day)
    days = [d for d in span if d not in already]
    workday = span[0] if span[0] in days else None
    rest_days = [d for d in days if d != workday]
    targets = (store.get_targets(workday) or set()) if workday else set()
    leaves = store.leaves_on(workday) if workday else {}  # 요약을 만드는 시각의 등록 상태
    reports = store.reports_on(days)

    def name(uid: str) -> str:
        member = members.get(uid)
        return member.name if member else uid

    workday_reports = {r.user_id: r for r in reports if r.report_date == workday}
    submitted, missing, annual, half_missing, on_leave = [], [], [], [], []
    for uid in targets:
        leave = leaves.get(uid)
        if uid in workday_reports:
            submitted.append(uid)
            if leave == ANNUAL:
                on_leave.append(name(uid))
        elif leave == ANNUAL:
            annual.append(name(uid))
        elif leave == HALF:
            half_missing.append(name(uid))
        else:
            missing.append(name(uid))

    rest = [r for r in reports if r.report_date != workday]
    entries: dict[str, list[tuple[date, str]]] = {}
    for r in reports:
        if r.report_date == workday and r.user_id not in targets:
            continue  # 그날 대상자가 아니었던 사람의 근무일 보고는 넣지 않는다
        entries.setdefault(r.user_id, []).append((r.report_date, r.content))
    people = [PersonReports(uid, name(uid), sorted(e)) for uid, e in entries.items()]

    return SummaryData(
        workday=workday,
        rest_days=rest_days,
        days=days,
        submitted=len(submitted),
        total=len(targets) - len(annual) - len(half_missing),
        missing=sorted(missing),
        annual=sorted(annual),
        half_missing=sorted(half_missing),
        submitted_on_leave=sorted(on_leave),
        rest_reports=sorted((name(r.user_id), r.report_date) for r in rest),
        people=sorted(people, key=lambda p: (p.name, p.user_id)),
        request_missed=workday is not None and not store.has_job_run("request", workday),
    )
