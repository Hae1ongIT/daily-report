from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from daily_report.db import ANNUAL, HALF, Store
from daily_report.errors import RuleError
from daily_report.reports import default_date, delete_report, submit_report
from daily_report.workcal import WorkCalendar

KST = ZoneInfo("Asia/Seoul")
W = 5
SUMMARY = time(10, 0)
CUR = {"U1", "U2"}


def d(s):
    return date.fromisoformat(s)


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=KST)


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def cal():
    return WorkCalendar()


def submit(store, cal, day, content="작업", now="2026-10-28T18:00", user="U1", existing=None):
    return submit_report(store, cal, user, d(day), content, at(now), W, CUR, existing)


# 작성·수정


def test_보고를_새로_낸다(store, cal):
    result = submit(store, cal, "2026-10-28")

    assert result.action == "created"
    assert store.get_report("U1", d("2026-10-28")).content == "작업"


def test_업무_내용이_비어_있으면_오류(store, cal):
    with pytest.raises(RuleError) as e:
        submit(store, cal, "2026-10-28", content="   ")

    assert e.value.field == "content"


def test_미래_날짜는_고를_수_없다(store, cal):
    with pytest.raises(RuleError, match="미래"):
        submit(store, cal, "2026-10-29")


def test_이미_보고가_있는_날짜로_새로_내면_덮어쓰지_않고_오류(store, cal):
    submit(store, cal, "2026-10-28", content="원래")

    with pytest.raises(RuleError, match="이미 보고가 있어요"):
        submit(store, cal, "2026-10-28", content="새로")

    assert store.get_report("U1", d("2026-10-28")).content == "원래"


def test_불러온_보고를_같은_날짜로_내면_수정된다(store, cal):
    rid = submit(store, cal, "2026-10-28", content="원래").report.id

    result = submit(store, cal, "2026-10-28", content="고침", existing=rid, now="2026-10-29T09:00")

    assert result.action == "updated"
    r = store.get_report("U1", d("2026-10-28"))
    assert r.content == "고침" and r.updated_at == at("2026-10-29T09:00")


def test_수정_창에서_비어_있는_날로_바꾸면_그_날짜로_옮긴다(store, cal):
    rid = submit(store, cal, "2026-10-27", now="2026-10-28T18:00").report.id

    result = submit(store, cal, "2026-10-28", existing=rid)

    assert result.action == "moved" and result.old_date == d("2026-10-27")
    assert store.get_report("U1", d("2026-10-27")) is None
    assert store.get_report("U1", d("2026-10-28")).id == rid


def test_수정_창에서_보고가_있는_날로_바꾸면_오류(store, cal):
    rid = submit(store, cal, "2026-10-27").report.id
    submit(store, cal, "2026-10-28")

    with pytest.raises(RuleError, match="이미 보고가 있어요"):
        submit(store, cal, "2026-10-28", existing=rid)


def test_다른_사람의_보고는_고칠_수_없다(store, cal):
    rid = submit(store, cal, "2026-10-28", user="U2").report.id

    with pytest.raises(RuleError, match="본인"):
        submit(store, cal, "2026-10-28", user="U1", existing=rid)


def test_수정_가능_기간이_지난_날짜는_내거나_고칠_수_없다(store, cal):
    # 10/20(화)의 기간은 10/27까지
    with pytest.raises(RuleError, match="수정 가능 기간"):
        submit(store, cal, "2026-10-20", now="2026-10-28T09:00")


def test_기간이_지난_보고는_다른_날짜로_옮길_수_없다(store, cal):
    rid = submit(store, cal, "2026-10-20", now="2026-10-20T18:00").report.id

    with pytest.raises(RuleError, match="수정 가능 기간"):
        submit(store, cal, "2026-10-28", existing=rid, now="2026-10-28T18:00")


def test_대상이_아니었던_날에는_낼_수_없다(store, cal):
    store.save_targets(d("2026-10-27"), {"U2"})

    with pytest.raises(RuleError, match="보고 대상이 아니었어요"):
        submit(store, cal, "2026-10-27", user="U1")


def test_쉬는_날에도_지금_대상자면_낼_수_있다(store, cal):
    result = submit(store, cal, "2026-10-31", now="2026-10-31T15:00")

    assert result.action == "created"


def test_연차인_날에도_낼_수_있다(store, cal):
    store.add_leaves("U1", [d("2026-10-28")], ANNUAL)

    assert submit(store, cal, "2026-10-28").action == "created"


# 삭제


def test_본인_보고를_지운다(store, cal):
    rid = submit(store, cal, "2026-10-28").report.id

    deleted = delete_report(store, cal, "U1", rid, at("2026-10-29T09:00"), W)

    assert deleted.id == rid
    assert store.get_report_by_id(rid) is None


def test_다른_사람_보고는_지울_수_없다(store, cal):
    rid = submit(store, cal, "2026-10-28", user="U2").report.id

    with pytest.raises(RuleError, match="본인"):
        delete_report(store, cal, "U1", rid, at("2026-10-29T09:00"), W)


def test_수정_가능_기간이_지난_보고는_지울_수_없다(store, cal):
    rid = submit(store, cal, "2026-10-20", now="2026-10-20T18:00").report.id

    with pytest.raises(RuleError, match="수정 가능 기간"):
        delete_report(store, cal, "U1", rid, at("2026-10-28T09:00"), W)


# 앱 홈 버튼의 기본 날짜


def default(store, cal, now, user="U1", current=CUR):
    return default_date(store, cal, user, at(now), SUMMARY, W, current)


def test_근무일_요약_시각_이후에_열면_당일(store, cal):
    assert default(store, cal, "2026-10-28T10:00") == d("2026-10-28")


def test_요약_시각_전에_열면_직전_근무일_보고가_없을_때_그날(store, cal):
    assert default(store, cal, "2026-10-28T09:30") == d("2026-10-27")


def test_요약_시각_전이라도_직전_근무일_보고가_있으면_당일(store, cal):
    submit(store, cal, "2026-10-27", now="2026-10-27T18:00")

    assert default(store, cal, "2026-10-28T09:30") == d("2026-10-28")


def test_쉬는_날에_열면_직전_근무일_기준(store, cal):
    assert default(store, cal, "2026-10-31T15:00") == d("2026-10-30")

    submit(store, cal, "2026-10-30", now="2026-10-30T18:00")
    assert default(store, cal, "2026-10-31T15:00") == d("2026-10-31")


def test_연차일은_건너뛴다(store, cal):
    store.add_leaves("U1", [d("2026-11-02")], ANNUAL)  # 월 연차, 금요일 보고 없음

    assert default(store, cal, "2026-11-03T09:30") == d("2026-10-30")


def test_보고_없는_반차일은_건너뛴다(store, cal):
    store.add_leaves("U1", [d("2026-10-27")], HALF)  # 화 반차, 보고 없음
    submit(store, cal, "2026-10-26", now="2026-10-26T18:00")  # 월 보고 있음

    assert default(store, cal, "2026-10-28T09:30") == d("2026-10-28")


def test_보고를_낸_반차일은_후보로_남아_당일이_된다(store, cal):
    store.add_leaves("U1", [d("2026-10-27")], HALF)
    submit(store, cal, "2026-10-27", now="2026-10-27T13:00")
    # 월요일은 보고가 없지만, 반차일(화)에서 멈추므로 당일

    assert default(store, cal, "2026-10-28T09:30") == d("2026-10-28")


def test_대상이_아니었던_날은_건너뛴다(store, cal):
    store.save_targets(d("2026-10-27"), {"U2"})  # U1은 화요일에 대상 아님
    submit(store, cal, "2026-10-26", now="2026-10-26T18:00")

    assert default(store, cal, "2026-10-28T09:30") == d("2026-10-28")


def test_후보가_수정_가능_기간을_넘었으면_당일(store, cal):
    # 10/22~10/27 모두 연차 → 남은 후보 10/21(수)의 기간은 10/28까지라 아직 됨
    store.add_leaves(
        "U1", [d(x) for x in ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]], ANNUAL
    )
    assert default(store, cal, "2026-10-28T09:30") == d("2026-10-21")

    store.add_leaves("U1", [d("2026-10-21")], ANNUAL)  # 다음 후보 10/20은 기간이 지남
    assert default(store, cal, "2026-10-28T09:30") == d("2026-10-28")


def test_동시에_두_번_제출돼_DB가_중복을_막으면_이미_있다는_오류로_바꾼다(store, cal, monkeypatch):
    import sqlite3

    def dup(*a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(store, "add_report", dup)

    with pytest.raises(RuleError, match="이미 보고가 있어요"):
        submit(store, cal, "2026-10-28")


def test_수정_가능_기간이_지난_날짜_오류에는_그_날짜를_적는다():
    from daily_report.reports import _require_editable

    with pytest.raises(RuleError, match="10월 19일은 수정 가능 기간이 지났어요"):
        _require_editable(WorkCalendar(), date(2026, 10, 19), at("2026-10-28T12:00"), 5)


def test_오늘_대상자_목록에_없으면_오늘을_기본_날짜로_주지_않는다(store, cal):
    # 17:30 목록이 정해진 뒤 들어온 사람: 오늘은 제출할 수 없는 날이다
    store.save_targets(d("2026-10-28"), {"U2"})

    got = default_date(store, cal, "U1", at("2026-10-28T18:30"), SUMMARY, W, CUR)

    assert got == d("2026-10-27")
