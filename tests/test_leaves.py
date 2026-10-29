from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from daily_report.db import ANNUAL, HALF, Store
from daily_report.errors import RuleError
from daily_report.leaves import cancel_leave, register_leave
from daily_report.workcal import WorkCalendar

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=KST)  # 수
W = 5


def d(s):
    return date.fromisoformat(s)


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def cal():
    return WorkCalendar()


def register(store, cal, start, end=None, kind=ANNUAL, user="U1", now=NOW):
    return register_leave(store, cal, user, d(start), d(end) if end else None, kind, now, W)


def test_연차_하루를_등록한다(store, cal):
    register(store, cal, "2026-11-04")

    assert store.get_leave("U1", d("2026-11-04")) == ANNUAL


def test_기간으로_등록하면_쉬는_날은_빼고_근무일만_등록한다(store, cal):
    days = register(store, cal, "2026-10-30", "2026-11-03")  # 금~화

    assert days == [d("2026-10-30"), d("2026-11-02"), d("2026-11-03")]
    assert store.get_leave("U1", d("2026-10-31")) is None


def test_하루만_등록할_때_쉬는_날을_고르면_오류(store, cal):
    with pytest.raises(RuleError, match="쉬는 날"):
        register(store, cal, "2026-10-31")
    with pytest.raises(RuleError, match="쉬는 날"):
        register(store, cal, "2026-10-31", kind=HALF)


def test_반차는_하루_단위로만_등록한다(store, cal):
    with pytest.raises(RuleError, match="하루"):
        register(store, cal, "2026-11-02", "2026-11-03", kind=HALF)

    register(store, cal, "2026-11-02", "2026-11-02", kind=HALF)
    assert store.get_leave("U1", d("2026-11-02")) == HALF


def test_기간_안에_근무일이_없으면_오류(store, cal):
    with pytest.raises(RuleError):
        register(store, cal, "2026-10-31", "2026-11-01")


def test_시작일이_종료일보다_늦으면_오류(store, cal):
    with pytest.raises(RuleError, match="종료일"):
        register(store, cal, "2026-11-03", "2026-11-02")


def test_이미_등록된_날에는_덮어쓰지_않고_오류(store, cal):
    register(store, cal, "2026-11-05", kind=HALF)

    with pytest.raises(RuleError, match="11월 5일은 이미 반차로 등록돼 있어요"):
        register(store, cal, "2026-11-05")
    assert store.get_leave("U1", d("2026-11-05")) == HALF


def test_기간에_겹치는_날이_있으면_그_날짜를_알려주고_전체를_등록하지_않는다(store, cal):
    register(store, cal, "2026-11-05", kind=HALF)
    register(store, cal, "2026-11-06")

    with pytest.raises(RuleError) as e:
        register(store, cal, "2026-11-04", "2026-11-09")

    assert "11월 5일" in e.value.message and "11월 6일" in e.value.message
    assert store.get_leave("U1", d("2026-11-04")) is None
    assert store.get_leave("U1", d("2026-11-09")) is None


def test_다른_사람의_연차와는_겹치지_않는다(store, cal):
    register(store, cal, "2026-11-04", user="U2")

    register(store, cal, "2026-11-04", user="U1")

    assert store.leaves_on(d("2026-11-04")) == {"U1": ANNUAL, "U2": ANNUAL}


def test_지난_날짜는_수정_가능_기간_안에서만_등록한다(store, cal):
    # 10/21(수)의 기간은 10/28까지 → 등록 가능
    register(store, cal, "2026-10-21")
    # 10/20(화)의 기간은 10/27까지 → 지남
    with pytest.raises(RuleError, match="수정 가능 기간"):
        register(store, cal, "2026-10-20")


def test_미래는_제한_없이_등록한다(store, cal):
    register(store, cal, "2027-03-02")

    assert store.get_leave("U1", d("2027-03-02")) == ANNUAL


def test_취소는_기간으로_지정하고_일부만_취소할_수_있다(store, cal):
    register(store, cal, "2026-11-02", "2026-11-06")

    cancelled = cancel_leave(store, cal, "U1", d("2026-11-05"), d("2026-11-06"), NOW, W)

    assert cancelled == [d("2026-11-05"), d("2026-11-06")]
    assert store.get_leave("U1", d("2026-11-04")) == ANNUAL
    assert store.get_leave("U1", d("2026-11-05")) is None


def test_하루만_취소할_수_있다(store, cal):
    register(store, cal, "2026-11-02", "2026-11-03")

    cancel_leave(store, cal, "U1", d("2026-11-03"), None, NOW, W)

    assert store.get_leave("U1", d("2026-11-03")) is None
    assert store.get_leave("U1", d("2026-11-02")) == ANNUAL


def test_취소할_연차가_없으면_오류(store, cal):
    with pytest.raises(RuleError, match="취소할"):
        cancel_leave(store, cal, "U1", d("2026-11-03"), None, NOW, W)


def test_지난_날짜는_수정_가능_기간_안에서만_취소한다(store, cal):
    store.add_leaves("U1", [d("2026-10-20")], ANNUAL)

    with pytest.raises(RuleError, match="수정 가능 기간"):
        cancel_leave(store, cal, "U1", d("2026-10-20"), None, NOW, W)


def test_동시에_등록돼_DB가_중복을_막으면_이미_등록됐다는_오류로_바꾼다(store, cal, monkeypatch):
    import sqlite3

    def dup(*a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(store, "add_leaves", dup)

    with pytest.raises(RuleError, match="이미 등록"):
        register(store, cal, "2026-11-04")


def test_날짜_뒤_조사는_받침과_상관없이_맞게_쓴다(store, cal):
    with pytest.raises(RuleError, match="10월 20일은 수정 가능 기간이 지나서"):
        register(store, cal, "2026-10-20")


# 앱 홈의 예정된 연차·반차 (SPEC 결정)


def test_내일_이후_연차_반차를_연속된_근무일끼리_묶는다():
    from daily_report.home import upcoming_leaves

    store = Store(":memory:")
    # 10/30(금)~11/2(월)은 주말을 건너 이어진 연차, 11/3(화)은 반차, 11/5(목)은 따로 연차
    store.add_leaves("U1", [date(2026, 10, 30), date(2026, 11, 2)], ANNUAL)
    store.add_leaves("U1", [date(2026, 11, 3)], HALF)
    store.add_leaves("U1", [date(2026, 11, 5)], ANNUAL)
    store.add_leaves("U1", [date(2026, 10, 28)], ANNUAL)  # 오늘은 날짜 목록에 나오므로 뺀다
    store.add_leaves("U2", [date(2026, 11, 4)], ANNUAL)

    got = upcoming_leaves(store, WorkCalendar(), "U1", date(2026, 10, 28))

    assert got == [
        (date(2026, 10, 30), date(2026, 11, 2), ANNUAL),
        (date(2026, 11, 3), date(2026, 11, 3), HALF),
        (date(2026, 11, 5), date(2026, 11, 5), ANNUAL),
    ]


def test_쉬는_날이_된_날의_연차는_예정에서_빼고_반차는_묶지_않는다():
    from daily_report.home import upcoming_leaves

    store = Store(":memory:")
    store.add_leaves("U1", [date(2026, 11, d) for d in (2, 3, 4)], ANNUAL)
    store.add_leaves("U1", [date(2026, 11, 9), date(2026, 11, 10)], HALF)
    cal = WorkCalendar({date(2026, 11, 3): "off"})  # 연차 등록 뒤 휴무일로 바뀜

    got = upcoming_leaves(store, cal, "U1", date(2026, 10, 28))

    assert got == [
        (date(2026, 11, 2), date(2026, 11, 4), ANNUAL),
        (date(2026, 11, 9), date(2026, 11, 9), HALF),
        (date(2026, 11, 10), date(2026, 11, 10), HALF),
    ]


# 관리 메뉴의 연차·반차 현황 (SPEC 결정)


def test_팀_연차_현황은_기간_안의_연차_반차를_사람별로_묶는다():
    from daily_report.home import team_leaves

    store = Store(":memory:")
    store.add_leaves("U1", [date(2026, 10, 28)], ANNUAL)  # 오늘도 넣는다
    store.add_leaves("U1", [date(2026, 10, 30), date(2026, 11, 2)], ANNUAL)
    store.add_leaves("U2", [date(2026, 11, 3)], HALF)
    store.add_leaves("U2", [date(2026, 10, 27)], ANNUAL)  # 지난 날은 뺀다
    store.add_leaves("U3", [date(2026, 11, 26)], ANNUAL)  # 기간 밖은 뺀다

    got = team_leaves(store, WorkCalendar(), date(2026, 10, 28), date(2026, 11, 24))

    assert got == {
        "U1": [
            (date(2026, 10, 28), date(2026, 10, 28), ANNUAL),
            (date(2026, 10, 30), date(2026, 11, 2), ANNUAL),
        ],
        "U2": [(date(2026, 11, 3), date(2026, 11, 3), HALF)],
    }
