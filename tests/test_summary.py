from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from daily_report.db import ANNUAL, HALF, Store
from daily_report.members import Member
from daily_report.summary import collect_summary
from daily_report.workcal import WorkCalendar

KST = ZoneInfo("Asia/Seoul")
T = datetime(2026, 10, 30, 18, 0, tzinfo=KST)
FRI = date(2026, 10, 30)
SAT = date(2026, 10, 31)
SUN = date(2026, 11, 1)
MON = date(2026, 11, 2)


def mem(uid, display=""):
    return Member(uid, display_name=display, real_name=f"실명{uid}")


MEMBERS = {u: mem(u, u.lower()) for u in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]}


@pytest.fixture
def store():
    return Store(":memory:")


def collect(store, day=MON):
    return collect_summary(store, WorkCalendar(), day, MEMBERS)


def test_SPEC_예시대로_제출_현황을_센다(store):
    # 대상 11명: a,b 미제출 / c 연차 / d 반차(미제출) / 나머지 7명 제출 + e 반차 제출
    store.save_targets(FRI, set(MEMBERS))
    store.add_leaves("C", [FRI], ANNUAL)
    store.add_leaves("D", [FRI], HALF)
    store.add_leaves("E", [FRI], HALF)
    for u in ["E", "F", "G", "H", "I", "J", "K"]:
        store.add_report(u, FRI, f"{u} 작업", T)

    s = collect(store)

    assert s.workday == FRI
    assert (s.submitted, s.total) == (7, 9)
    assert s.missing == ["a", "b"]
    assert s.annual == ["c"]
    assert s.half_missing == ["d"]


def test_연차인_날에_보고가_있으면_제출로_세고_표시한다(store):
    store.save_targets(FRI, {"A", "B"})
    store.add_leaves("A", [FRI], ANNUAL)
    store.add_report("A", FRI, "연차 중 장애 대응", T)

    s = collect(store)

    assert (s.submitted, s.total) == (1, 2)
    assert s.annual == []
    assert s.submitted_on_leave == ["a"]
    assert s.missing == ["b"]


def test_대상자_목록에_없는_사람의_근무일_보고는_넣지_않는다(store):
    store.save_targets(FRI, {"A"})
    store.add_report("A", FRI, "a 작업", T)
    store.add_report("B", FRI, "대상 아님", T)

    s = collect(store)

    assert (s.submitted, s.total) == (1, 1)
    assert [p.name for p in s.people] == ["a"]


def test_쉬는_날_보고는_따로_표시하고_사람별_내용에_날짜와_함께_넣는다(store):
    store.save_targets(FRI, {"A", "B"})
    store.add_report("A", FRI, "API 설계 마무리", T)
    store.add_report("A", SAT, "장애 대응", T)
    store.add_report("C", SUN, "배포 점검", T)

    s = collect(store)

    assert s.rest_reports == [("a", SAT), ("c", SUN)]
    a = next(p for p in s.people if p.name == "a")
    assert a.entries == [(FRI, "API 설계 마무리"), (SAT, "장애 대응")]
    assert [p.name for p in s.people] == ["a", "c"]


def test_연차_반차_여부는_요약을_만들_때의_등록_상태를_따른다(store):
    store.save_targets(FRI, {"A"})
    # 17:30 목록 저장 뒤에 연차를 나중에 등록
    store.add_leaves("A", [FRI], ANNUAL)

    s = collect(store)

    assert s.annual == ["a"] and s.missing == [] and s.total == 0


def test_이름은_표시_이름이고_없으면_실명이다(store):
    members = {"A": mem("A", ""), "B": mem("B", "비")}
    store.save_targets(FRI, {"A", "B"})

    s = collect_summary(store, WorkCalendar(), MON, members)

    assert s.missing == ["비", "실명A"]


def test_목록이_없는_사용자_ID는_ID로_표시한다(store):
    store.save_targets(FRI, {"UNKNOWN"})

    assert collect(store).missing == ["UNKNOWN"]


def test_표시_이름이_같아도_사람별_보고는_따로_모은다(store):
    members = {"A": mem("A", "민수"), "B": mem("B", "민수")}
    store.save_targets(FRI, {"A", "B"})
    store.add_report("A", FRI, "a 작업", T)
    store.add_report("B", FRI, "b 작업", T)

    s = collect_summary(store, WorkCalendar(), MON, members)

    assert [(p.user_id, p.name) for p in s.people] == [("A", "민수"), ("B", "민수")]


def test_이미_요약한_근무일은_빼고_남은_쉬는_날_보고만_모은다(store):
    # 금요일은 이미 요약됨. 그 뒤 월요일을 휴무일로 바꿔서 월요일 보고가 쉬는 날 보고가 됨
    store.save_targets(FRI, {"A"})
    store.add_report("A", FRI, "금 작업", T)
    store.add_report("B", MON, "월 작업", T)
    cal = WorkCalendar({MON: "off"})
    tue = date(2026, 11, 3)

    s = collect_summary(store, cal, tue, MEMBERS, already={FRI})

    assert s.workday is None
    assert (s.submitted, s.total, s.missing) == (0, 0, [])
    assert s.days == [SAT, SUN, MON]
    assert [p.name for p in s.people] == ["b"]
    assert s.rest_reports == [("b", MON)]


def test_그날_보고_요청을_다_보내지_못했으면_표시한다(store):
    store.save_targets(FRI, {"A"})

    assert collect(store).request_missed

    store.mark_job_run("request", FRI, T)
    assert not collect(store).request_missed


def test_이미_요약한_근무일이면_요청_누락을_따지지_않는다(store):
    s = collect_summary(store, WorkCalendar(), MON, MEMBERS, already={FRI})

    assert not s.request_missed
