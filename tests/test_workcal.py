from datetime import date, datetime
from zoneinfo import ZoneInfo

from daily_report.workcal import OFF, WORK, WorkCalendar

KST = ZoneInfo("Asia/Seoul")


def d(s: str) -> date:
    return date.fromisoformat(s)


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=KST)


def test_평일은_근무일이고_주말과_공휴일은_쉰다():
    cal = WorkCalendar()

    assert cal.is_workday(d("2026-10-28"))  # 수
    assert not cal.is_workday(d("2026-10-31"))  # 토
    assert not cal.is_workday(d("2026-11-01"))  # 일
    assert not cal.is_workday(d("2026-12-25"))  # 성탄절(금)


def test_관리자가_추가한_휴무일은_쉰다():
    cal = WorkCalendar({d("2026-10-28"): OFF})

    assert not cal.is_workday(d("2026-10-28"))


def test_주말과_공휴일을_출근일로_지정할_수_있다():
    cal = WorkCalendar({d("2026-10-31"): WORK, d("2026-12-25"): WORK})

    assert cal.is_workday(d("2026-10-31"))
    assert cal.is_workday(d("2026-12-25"))


def test_직전_근무일은_쉬는_날을_건너뛴다():
    cal = WorkCalendar()

    assert cal.prev_workday(d("2026-11-02")) == d("2026-10-30")  # 월 → 금
    assert cal.prev_workday(d("2026-12-28")) == d("2026-12-24")  # 성탄절·주말 건너뜀


def test_다음_근무일은_쉬는_날을_건너뛴다():
    cal = WorkCalendar()

    assert cal.next_workday(d("2026-10-30")) == d("2026-11-02")
    assert cal.next_workday(d("2026-10-31")) == d("2026-11-02")  # 쉬는 날에서 시작
    assert cal.next_workday(d("2026-12-24")) == d("2026-12-28")


def test_수정_가능_기간은_다음_근무일부터_5근무일째까지다():
    cal = WorkCalendar()

    assert cal.edit_last_day(d("2026-10-26"), 5) == d("2026-11-02")  # 월 → 다음 주 월
    # 12/25(성탄절), 주말, 1/1(신정)을 세지 않는다
    assert cal.edit_last_day(d("2026-12-24"), 5) == d("2027-01-04")


def test_쉬는_날_보고의_수정_가능_기간도_다음_근무일부터_센다():
    cal = WorkCalendar()

    assert cal.edit_last_day(d("2026-10-31"), 5) == d("2026-11-06")  # 토 → 다음 주 금


def test_수정_가능_기간은_마지막_날_자정까지다():
    cal = WorkCalendar()

    assert cal.is_editable(d("2026-10-26"), at("2026-11-02T23:59:59"), 5)
    assert not cal.is_editable(d("2026-10-26"), at("2026-11-03T00:00:00"), 5)


def test_미래_날짜는_수정_가능_기간이_아니다():
    cal = WorkCalendar()

    assert not cal.is_editable(d("2026-10-29"), at("2026-10-28T12:00:00"), 5)


def test_요약_범위는_직전_근무일과_그_뒤_쉬는_날들이다():
    cal = WorkCalendar()

    assert cal.summary_range(d("2026-10-28")) == [d("2026-10-27")]
    assert cal.summary_range(d("2026-11-02")) == [d("2026-10-30"), d("2026-10-31"), d("2026-11-01")]
    assert cal.summary_range(d("2026-12-28")) == [
        d("2026-12-24"),
        d("2026-12-25"),
        d("2026-12-26"),
        d("2026-12-27"),
    ]


def test_토요일을_출근일로_지정하면_요약_범위가_나뉜다():
    cal = WorkCalendar({d("2026-10-31"): WORK})

    assert cal.summary_range(d("2026-10-31")) == [d("2026-10-30")]
    assert cal.summary_range(d("2026-11-02")) == [d("2026-10-31"), d("2026-11-01")]
