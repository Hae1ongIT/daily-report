from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from daily_report.db import Store
from daily_report.errors import RuleError
from daily_report.members import Member
from daily_report.targets import (
    add_off_day,
    current_targets,
    ensure_targets,
    remove_off_day,
    set_work_day,
    was_target,
)
from daily_report.workcal import WorkCalendar

KST = ZoneInfo("Asia/Seoul")
WED = date(2026, 10, 28)
SAT = date(2026, 10, 31)


def m(uid, **kw):
    return Member(id=uid, display_name=uid, real_name=uid, **kw)


MEMBERS = [
    m("U1"),
    m("U2"),
    m("BOT", is_bot=True),
    m("GUEST", is_guest=True),
    m("GONE", deleted=True),
]


@pytest.fixture
def store():
    return Store(":memory:")


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=KST)


def test_봇_게스트_비활성_계정은_대상이_아니다():
    assert current_targets(MEMBERS, exclusions=set(), test_users=()) == {"U1", "U2"}


def test_제외_명단에_있는_사람은_대상이_아니다():
    assert current_targets(MEMBERS, exclusions={"U2"}, test_users=()) == {"U1"}


def test_시험_모드에서는_시험_대상자만_대상이다():
    members = [*MEMBERS, m("U3")]

    assert current_targets(members, exclusions=set(), test_users=("U1", "U3")) == {"U1", "U3"}


def test_시험_모드에서도_제외_명단과_봇은_빠진다():
    assert current_targets(MEMBERS, exclusions={"U1"}, test_users=("U1", "U2", "BOT")) == {"U2"}


def test_목록이_없으면_지금_대상자로_만들어_저장한다(store):
    got = ensure_targets(store, WED, lambda: {"U1", "U2"})

    assert got == {"U1", "U2"}
    assert store.get_targets(WED) == {"U1", "U2"}


def test_목록이_이미_있으면_그대로_쓴다(store):
    store.save_targets(WED, {"U1"})

    got = ensure_targets(store, WED, lambda: {"U1", "U2"})

    assert got == {"U1"}


def test_근무일에는_그날_목록에_있었는지로_판단한다(store):
    cal = WorkCalendar()
    store.save_targets(WED, {"U1"})

    assert was_target(store, cal, "U1", WED, current={"U2"})
    assert not was_target(store, cal, "U2", WED, current={"U2"})


def test_목록이_아직_없는_날은_지금_대상자인지로_판단한다(store):
    cal = WorkCalendar()

    assert was_target(store, cal, "U2", WED, current={"U2"})
    assert not was_target(store, cal, "U1", WED, current={"U2"})


def test_쉬는_날은_지금_대상자면_보고할_수_있다(store):
    cal = WorkCalendar()

    assert was_target(store, cal, "U1", SAT, current={"U1"})
    assert not was_target(store, cal, "U9", SAT, current={"U1"})


def test_휴무일을_추가하면_그날_대상자_목록을_지운다(store):
    store.save_targets(WED, {"U1"})

    add_off_day(store, WED, now=at("2026-10-28T18:00"), edit_window=5)

    assert store.overrides() == {WED: "off"}
    assert store.get_targets(WED) is None


def test_휴무일_추가는_수정_가능_기간_안의_지난_날짜까지_된다(store):
    # 10/23(금)의 기간은 10/30까지라 11/2에는 지났다
    with pytest.raises(RuleError):
        add_off_day(store, date(2026, 10, 23), now=at("2026-11-02T10:00"), edit_window=5)

    # 10/26(월)의 기간은 11/2까지
    add_off_day(store, date(2026, 10, 26), now=at("2026-11-02T10:00"), edit_window=5)
    assert store.overrides() == {date(2026, 10, 26): "off"}


def test_출근일_지정은_내일_이후만_된다(store):
    set_work_day(store, SAT, now=at("2026-10-30T12:00"))

    assert store.overrides() == {SAT: "work"}
    with pytest.raises(RuleError):
        set_work_day(store, SAT, now=at("2026-10-31T08:00"))


def test_관리자가_추가한_휴무일을_취소하면_출근일로_바뀌므로_내일_이후만_된다(store):
    store.set_override(WED, "off")

    with pytest.raises(RuleError):
        remove_off_day(store, WED, now=at("2026-10-28T08:00"))

    remove_off_day(store, WED, now=at("2026-10-27T08:00"))
    assert store.overrides() == {}


def test_추가한_휴무일_취소가_안_될_때는_휴무일_취소로_안내한다(store):
    store.set_override(WED, "off")

    with pytest.raises(RuleError, match="휴무일 취소"):
        remove_off_day(store, WED, now=at("2026-10-28T08:00"))


def test_봇을_처음_켠_날_이전은_대상이_아니었던_날로_본다(store):
    cal = WorkCalendar()
    store.mark_job_run("installed", WED, at("2026-10-28T08:00"))

    assert not was_target(store, cal, "U1", date(2026, 10, 27), current={"U1"})
    assert was_target(store, cal, "U1", WED, current={"U1"})


def test_대상자가_없으면_목록을_저장하지_않는다(store):
    with pytest.raises(RuntimeError, match="대상자"):
        ensure_targets(store, WED, lambda: set())

    assert store.get_targets(WED) is None


def test_사용자_목록을_새로_읽는_동안_DB를_막지_않는다(store):
    import threading

    free = []

    def current():
        t = threading.Thread(target=lambda: free.append(store.get_targets(SAT)))
        t.start()
        t.join(timeout=1)
        return {"U1"}

    ensure_targets(store, WED, current)

    assert free == [None]  # 다른 스레드가 기다리지 않고 DB를 읽었다


def test_출근일로_지정한_날을_휴무일_취소로_되돌리려_하면_출근일이라고_알린다(store):
    set_work_day(store, SAT, now=at("2026-10-28T12:00"))

    with pytest.raises(RuleError, match="출근일로 지정한 날"):
        remove_off_day(store, SAT, now=at("2026-10-28T12:00"))


def test_출근일_지정은_내일_이후만_취소할_수_있다(store):
    from daily_report.targets import remove_work_day

    store.set_override(SAT, "work")

    with pytest.raises(RuleError, match="내일 이후"):
        remove_work_day(store, SAT, now=at("2026-10-31T08:00"))

    remove_work_day(store, SAT, now=at("2026-10-30T12:00"))
    assert store.overrides() == {}


def test_출근일로_지정하지_않은_날은_출근일_지정을_취소할_수_없다(store):
    from daily_report.targets import remove_work_day

    store.set_override(SAT, "off")

    with pytest.raises(RuleError, match="출근일로 지정한 날이 아니에요"):
        remove_work_day(store, SAT, now=at("2026-10-28T12:00"))
