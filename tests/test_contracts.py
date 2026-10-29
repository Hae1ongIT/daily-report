"""경계값·연결 계약 테스트: 뮤테이션 리뷰에서 기존 테스트가 잡지 못한 규칙을 지킨다."""

import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from daily_report import views
from daily_report.db import ANNUAL, HALF, Store
from daily_report.errors import RuleError
from daily_report.home import home_rows
from daily_report.jobs import Jobs
from daily_report.members import Member
from daily_report.slack_lists import ListError, ListSync
from daily_report.summary import LlmSummary, PersonReports, SummaryData, collect_summary
from daily_report.targets import ensure_targets, remove_off_day, was_target
from daily_report.workcal import OFF, WorkCalendar
from tests.conftest import at
from tests.test_handlers import view
from tests.test_jobs import DEFAULT, FakeSummarizer, admin_posts, dms
from tests.test_slack_lists import FakeApi, add, field

KST = ZoneInfo("Asia/Seoul")
D = date.fromisoformat
WED, THU, FRI, SAT, SUN, MON = (
    D(x)
    for x in ("2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02")
)


@pytest.fixture
def setup(make_bot):
    def make(now, result=DEFAULT, **kw):
        bot, slack, store, lists, clock = make_bot(now=now, **kw)
        return Jobs(bot, FakeSummarizer(result)), bot, slack, store, lists, clock

    return make


# ---------------------------------------------------------------- 예약 작업 시각 경계


def test_요청_시한_21시_정각까지는_보낸다(setup):
    jobs, _, slack, *_ = setup("2026-10-28T21:00")
    jobs.request(at("2026-10-28T21:00:00"))
    assert len(dms(slack)) == 3


def test_요청_시각_17시30분_1초_전에는_보내지_않는다(setup):
    jobs, _, slack, *_ = setup("2026-10-28T17:29")
    jobs.request(at("2026-10-28T17:29:59"))
    assert slack.of("chat_postMessage") == []


def test_재알림_시한_10시_정각까지는_보낸다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T10:00")
    store.save_targets(WED, {"U1"})
    jobs.reminder(at("2026-10-29T10:00:00"))
    assert set(dms(slack)) == {"U1"}


def test_재알림_시각_9시_1초_전에는_보내지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T08:59")
    store.save_targets(WED, {"U1"})
    jobs.reminder(at("2026-10-29T08:59:59"))
    assert slack.of("chat_postMessage") == []


def test_요약_시한_18시_정각까지는_정상_요약을_올린다(setup):
    jobs, _, slack, store, _, _ = setup("2026-10-29T18:00")
    store.save_targets(WED, {"U1"})
    jobs.summary(at("2026-10-29T18:00:00"))
    assert not any("놓쳤어요" in p["text"] for p in admin_posts(slack))
    assert len(admin_posts(slack)) == 1


def test_요약_10시_1초_전에는_올리지_않는다(setup):
    jobs, _, slack, store, _, _ = setup("2026-10-29T09:59")
    store.save_targets(WED, {"U1"})
    jobs.summary(at("2026-10-29T09:59:59"))
    assert admin_posts(slack) == []


# ---------------------------------------------------------------- 한 번만 보낸다 (실행 기록)


def test_요청을_보낸_뒤_보고를_지운_사람에게는_다시_보내지_않는다(setup):
    # SPEC: 17:30 요청은 하루 한 번
    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    rid = store.add_report("U1", WED, "냄", at("2026-10-28T15:00"))
    jobs.request(at("2026-10-28T17:30"))
    store.delete_report(rid)

    jobs.request(at("2026-10-28T17:40"))

    assert "U1" not in dms(slack)


def test_재알림을_보낸_뒤_보고를_지운_사람에게는_다시_보내지_않는다(setup):
    # SPEC: 미제출자에게 DM으로 "한 번 더"
    jobs, _, slack, store, *_ = setup("2026-10-29T09:00")
    store.save_targets(WED, {"U1", "U2"})
    rid = store.add_report("U1", WED, "냄", at("2026-10-28T18:00"))
    jobs.reminder(at("2026-10-29T09:00"))
    store.delete_report(rid)

    jobs.reminder(at("2026-10-29T09:30"))

    assert "U1" not in dms(slack)


# ---------------------------------------------------------------- 요약 작업


def test_17시30분_작업을_건너뛰고_재알림도_없으면_요약이_대상자_목록을_만든다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T10:00")

    jobs.summary(at("2026-10-29T10:00"))

    assert store.get_targets(WED) == {"U1", "U2", "UADMIN"}
    assert "제출 0/3" in json.dumps(admin_posts(slack)[0]["blocks"], ensure_ascii=False)


def test_요약의_리스트_링크는_요약_대상_날짜가_속한_달이다(setup):
    # 11/2(월) 요약이 다루는 날짜는 10/30(금) -> 10월 리스트
    jobs, _, slack, store, *_ = setup("2026-11-02T10:00")
    store.save_targets(FRI, {"U1"})

    jobs.summary(at("2026-11-02T10:00"))

    body = json.dumps(admin_posts(slack)[0]["blocks"], ensure_ascii=False)
    assert "https://list/2026-10" in body


def test_리스트_링크는_요약_맨_아래_블록이다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T10:00")
    store.save_targets(WED, {"U1"})

    jobs.summary(at("2026-10-29T10:00"))

    assert "https://list/2026-10" in admin_posts(slack)[0]["blocks"][-1]["text"]["text"]


def test_요약할_날이_모두_이미_다뤄졌으면_올리지_않고_완료만_기록한다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T10:00")
    store.mark_job_run("summary_day", WED, at("2026-10-29T09:00"))

    jobs.summary(at("2026-10-29T10:00"))

    assert admin_posts(slack) == []
    assert store.has_job_run("summary", THU)


def test_설치한_날의_요약도_놓쳤으면_알린다(setup):
    # 10/28 설치, 10/29 요약(= 10/28 보고)을 못 했음 -> 10/30에 알림
    jobs, _, slack, store, *_ = setup("2026-10-30T10:00")
    store.mark_job_run("installed", WED, at("2026-10-28T09:00"))

    jobs.summary(at("2026-10-30T10:00"))

    assert (
        sum("놓쳤어요" in p["text"] and "10/28(수)" in p["text"] for p in admin_posts(slack)) == 1
    )


def test_놓침_알림은_이미_다룬_날을_빼고_첫_날짜를_알린다(setup):
    jobs, _, slack, store, *_ = setup("2026-11-02T18:30")
    store.mark_job_run("summary_day", FRI, at("2026-10-30T10:00"))  # 금요일 보고는 이미 다룸

    jobs.summary(at("2026-11-02T18:30"))

    text = admin_posts(slack)[0]["text"]
    assert "10/31(토)" in text and "10/30" not in text


def test_사흘_이상_꺼져_있었어도_모두_알린다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-30T10:00")
    store.mark_job_run("installed", D("2026-10-19"), at("2026-10-19T09:00"))

    jobs.summary(at("2026-10-30T10:00"))

    texts = [p["text"] for p in admin_posts(slack) if "놓쳤어요" in p["text"]]
    assert len(texts) == 1  # 한 메시지로 묶어 알린다
    assert all(x in texts[0] for x in ("10/28(수)", "10/27(화)", "10/26(월)"))


@pytest.mark.parametrize("month,expected", [("2026-03-02", "2026-03"), ("2027-01-04", "2027-01")])
def test_그_달_리스트_이름은_1_9월에도_두_자리_월이다(setup, month, expected):
    jobs, _, _, _, lists, _ = setup(f"{month}T09:00")
    jobs.monthly_list(at(f"{month}T09:00"))
    assert lists.ensured == [expected]


# ---------------------------------------------------------------- 보고 규칙


def test_업무_내용_앞뒤_공백은_잘라서_저장한다(make_bot):
    bot, _, store, *_ = make_bot()
    bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="  작업 \n"))
    assert store.get_report("U1", WED).content == "작업"


def test_핸들러가_수정_창의_보고_ID로_날짜를_옮긴다(make_bot):
    bot, _, store, lists, _ = make_bot()
    rid = store.add_report("U1", D("2026-10-27"), "원래", at("2026-10-27T18:00"))

    errors = bot.submit_report("U1", view({"existing_id": rid}, date="2026-10-28", content="옮김"))

    assert errors is None
    assert store.get_report("U1", D("2026-10-27")) is None
    moved = store.get_report("U1", WED)
    assert moved.id == rid and moved.content == "옮김"
    assert lists.upserted[-1] == rid


def test_핸들러가_수정_창의_보고를_같은_날짜로_고친다(make_bot):
    bot, _, store, lists, _ = make_bot(now="2026-10-29T09:00")
    rid = store.add_report("U1", WED, "원래", at("2026-10-28T18:00"))

    bot.submit_report("U1", view({"existing_id": rid}, date="2026-10-28", content="고침"))

    assert store.get_report("U1", WED).updated_at == at("2026-10-29T09:00")
    assert lists.upserted == [rid]


def test_다른_사람_보고_삭제_버튼은_거부하고_알린다(make_bot):
    bot, slack, store, lists, _ = make_bot()
    rid = store.add_report("U2", WED, "남의 보고", at("2026-10-28T17:00"))

    bot.delete_report("U1", str(rid))

    assert store.get_report_by_id(rid) is not None and lists.removed == []
    assert "본인" in slack.last("chat_postMessage")["text"]


# ---------------------------------------------------------------- 대상자 목록


def test_빈_대상자_목록도_목록이_있는_것으로_본다():
    store, cal = Store(":memory:"), WorkCalendar()
    store.save_targets(WED, set())

    assert was_target(store, cal, "U1", WED, current={"U1"}) is False
    assert ensure_targets(store, WED, lambda: {"U1"}) == set()  # 저장된 빈 목록이 우선


def test_쉬는_날은_저장된_목록이_있어도_지금_대상자로_본다():
    store, cal = Store(":memory:"), WorkCalendar()
    store.save_targets(SAT, {"U2"})  # 출근일이었다가 되돌린 경우 등

    assert was_target(store, cal, "U1", SAT, current={"U1"}) is True


def test_휴무일이_아닌_날의_취소는_오류():
    store = Store(":memory:")
    with pytest.raises(RuleError, match="휴무일이 아니"):
        remove_off_day(store, WED, now=at("2026-10-27T08:00"))


def test_원래_쉬는_날에_추가한_휴무일_표시는_오늘도_지울_수_있다():
    store = Store(":memory:")
    store.set_override(SAT, OFF)
    remove_off_day(store, SAT, now=at("2026-10-31T08:00"))  # 토요일 당일, 원래 쉬는 날
    assert store.overrides() == {}


def test_연차_취소는_종료일이_시작일보다_빠르면_종료일_칸_오류():
    from daily_report.leaves import cancel_leave

    store = Store(":memory:")
    with pytest.raises(RuleError) as e:
        cancel_leave(
            store, WorkCalendar(), "U1", D("2026-11-03"), D("2026-11-02"), at("2026-10-28T12:00"), 5
        )
    assert e.value.field == "end"


# ---------------------------------------------------------------- 요약 집계 정렬


def mem(uid, display):
    return Member(uid, display_name=display, real_name=uid)


def test_표시_이름이_같으면_ID_순으로_정렬한다():
    store = Store(":memory:")
    members = {"A": mem("A", "민수"), "B": mem("B", "민수")}
    store.save_targets(FRI, {"B"})
    store.add_report("B", FRI, "b", at("2026-10-30T18:00"))
    store.add_report("A", SAT, "a", at("2026-10-31T18:00"))  # A는 쉬는 날 보고만

    s = collect_summary(store, WorkCalendar(), MON, members)

    assert [p.user_id for p in s.people] == ["A", "B"]


def test_쉬는_날_보고자는_날짜가_아니라_이름순이다():
    store = Store(":memory:")
    members = {"A": mem("A", "a"), "C": mem("C", "c")}
    store.save_targets(FRI, set())
    store.add_report("C", SAT, "c", at("2026-10-31T18:00"))
    store.add_report("A", SUN, "a", at("2026-11-01T18:00"))

    s = collect_summary(store, WorkCalendar(), MON, members)

    assert s.rest_reports == [("a", SUN), ("c", SAT)]


@pytest.mark.parametrize("seed", range(5))
def test_미제출자_이름은_정렬되어_나온다(seed):
    store = Store(":memory:")
    names = "kjihgfedcba"
    members = {n.upper(): mem(n.upper(), n) for n in names}
    store.save_targets(FRI, set(members))

    s = collect_summary(store, WorkCalendar(), MON, members)

    assert s.missing == sorted(names)


# ---------------------------------------------------------------- 앱 홈 / 화면


def test_연차인_날에_낸_보고는_앱_홈에서_연차_표시가_붙는다():
    store, cal = Store(":memory:"), WorkCalendar()
    store.add_leaves("U1", [WED], ANNUAL)
    store.add_report("U1", WED, "연차 중 대응", at("2026-10-28T15:00"))

    rows = home_rows(store, cal, "U1", at("2026-10-28T18:00"), 5, {"U1"})

    assert rows[0].status == "submitted" and rows[0].on_leave is True
    text = json.dumps(views.app_home(True, False, rows[:1])["blocks"], ensure_ascii=False)
    assert "제출 (연차)" in text


def test_앱_홈_미리보기는_80자로_자르고_멘션으로_해석되지_않는_plain_text다():
    row = views.HomeRow(WED, "submitted", D("2026-11-04"), 3, "<@U1>" + "가" * 200)
    blocks = views.app_home(True, False, [row])["blocks"]
    plain = [b["text"] for b in blocks if b.get("text", {}).get("type") == "plain_text"]
    preview = plain[-1]["text"]
    # plain_text는 멘션·링크로 바뀌지 않는다. 80자 = "<@U1>" 5자 + 74자 + "…"
    assert preview == "<@U1>" + "가" * 74 + "…"


def test_삭제_확인_창에_날짜가_들어간다():
    row = views.HomeRow(WED, "submitted", D("2026-11-04"), 3, "x")
    text = json.dumps(views.app_home(True, False, [row])["blocks"], ensure_ascii=False)
    assert "10/28(수) 보고를 지울까요?" in text


def test_하루짜리_요약_제목에는_범위_표시가_없다():
    data = SummaryData(
        WED, [], [WED], 1, 1, [], [], [], [], [], [PersonReports("A", "a", [(WED, "x")])]
    )
    _, title = views.summary_message(data, LlmSummary("t", [("a", "s")]), [("10월", "u")])
    assert "~" not in title


def test_LLM_요약에_사람이_없으면_팀_요약과_표를_넣지_않는다():
    data = SummaryData(WED, [], [WED], 0, 0, [], [], [], [], [])
    blocks, _ = views.summary_message(data, LlmSummary("", []), [])
    body = json.dumps(blocks, ensure_ascii=False)
    assert "팀 요약" not in body and not any(b["type"] == "table" for b in blocks)


def test_사람이_많아도_표_글자수와_현황_길이가_Slack_한도_안이다():
    people = [(f"이름{i:03d}" * 5, "가" * 300) for i in range(100)]
    data = SummaryData(
        WED,
        [],
        [WED],
        0,
        100,
        [f"사람{i}" * 10 for i in range(300)],
        [],
        [],
        [],
        [],
        [PersonReports("A", "a", [(WED, "x")])],
    )
    blocks, _ = views.summary_message(data, LlmSummary("t", people), [("10월", "u")])

    table = next(b for b in blocks if b["type"] == "table")
    assert sum(len(c["text"]) for r in table["rows"] for c in r) <= 10000
    status = blocks[1]["text"]["text"]
    assert len(status) <= 3000


def test_모든_화면은_slack_sdk_검증을_통과한다():
    from slack_sdk.models.blocks import Block
    from slack_sdk.models.views import View

    rows = [
        views.HomeRow(WED, "submitted", D("2026-11-04"), 3, "x" * 500, True),
        views.HomeRow(THU, "missing", D("2026-11-04")),
        views.HomeRow(FRI, "half", D("2026-11-04")),
    ]
    for v in (
        views.report_modal(WED, "x", 1),
        views.leave_modal(True, True),
        views.leave_modal(False, False),
        views.exclusions_modal([]),
        views.calendar_modal(),
        views.sync_modal(),
        views.leave_status_modal(WED, WED, [("a", [(WED, WED, ANNUAL)])]),
        views.leave_status_modal(WED, WED, []),
        views.app_home(True, True, rows),
        views.app_home(False, True, []),
    ):
        View(**v).validate_json()
    for b in views.request_dm(WED) + views.reminder_dm(WED, time(10, 0)):
        Block.parse(b).validate_json()


# ---------------------------------------------------------------- 관리 메뉴


def test_동기화_기간이_하루여도_된다(make_bot):
    bot, *_ = make_bot()
    assert bot.validate_sync("UADMIN", view(start="2026-10-28", end="2026-10-28")) is None


def test_추가한_휴무일을_취소한다(make_bot):
    bot, _, store, *_ = make_bot(now="2026-10-28T18:00")
    store.set_override(D("2026-11-04"), "off")

    errors = bot.submit_calendar("UADMIN", view(day="2026-11-04", kind="remove_off"))

    assert errors is None and store.overrides() == {}


def test_관리자가_다른_사람_연차를_등록하면_그_사람의_앱_홈을_다시_그린다(make_bot):
    bot, slack, *_ = make_bot()
    meta = {"register": True, "for_other": True}

    bot.submit_leave(
        "UADMIN", view(meta, user="U2", kind="annual", start="2026-11-02"), ack=lambda e=None: None
    )

    assert slack.last("views_publish")["user_id"] == "U2"


def test_관리자_채널_멤버는_관리자_채널로_조회한다(make_bot):
    bot, slack, *_ = make_bot()
    bot.directory.is_admin("UADMIN")
    assert slack.last("conversations_members")["channel"] == "CADMIN"


def test_사용자와_관리자_목록은_여러_쪽을_모두_읽는다(make_bot):
    bot, slack, *_ = make_bot()
    pages = {None: (["UADMIN"], "c2"), "c2": (["UNEW"], "")}

    def members(**kw):
        ids, nxt = pages[kw.get("cursor")]
        return {"members": ids, "response_metadata": {"next_cursor": nxt}}

    slack.conversations_members = members
    assert bot.directory.is_admin("UNEW") is True


def test_갱신이_실패해도_이전_관리자_목록으로_앱_홈은_보여준다(make_bot):
    from datetime import timedelta

    bot, slack, _, _, clock = make_bot()
    assert bot.directory.is_admin("UADMIN")

    def boom(**kw):
        raise RuntimeError("down")

    slack.conversations_members = boom
    clock.at += timedelta(seconds=601)
    assert bot.directory.is_admin("UADMIN") is True  # 실행 시(fresh)에는 False


# ---------------------------------------------------------------- 리스트


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def sync(api):
    store = Store(":memory:")
    return ListSync(api, store, "CADMIN"), store


def test_1_9월_리스트도_동기화에서_찾는다(sync, api):
    ls, store = sync
    ls.upsert(add(store, day=D("2026-03-10")).id)
    ls.sync(D("2026-03-01"), D("2026-03-31"))
    assert "slackLists.items.list" in api.methods()


def test_연말_연초를_걸친_동기화는_두_해의_리스트를_모두_맞춘다(sync, api):
    ls, store = sync
    ls.upsert(add(store, day=D("2026-12-31"), user="U1").id)
    ls.upsert(add(store, day=D("2027-01-04"), user="U2").id)
    ls.sync(D("2026-12-31"), D("2027-01-04"))
    listed = [p["list_id"] for m, p in api.calls if m == "slackLists.items.list"]
    assert sorted(listed) == sorted([store.get_list_id("2026-12"), store.get_list_id("2027-01")])


def test_동기화는_끝날의_DB에_없는_행도_지운다(sync, api):
    ls, store = sync
    ls.upsert(add(store, day=WED).id)
    api.list_items["F1"] = [
        {"id": "Rec_orphan", "fields": [{"column_id": "Col_date", "date": ["2026-10-28"]}]}
    ]
    ls.sync(D("2026-10-27"), WED)
    assert [p["id"] for m, p in api.calls if m == "slackLists.items.delete"] == ["Rec_orphan"]


def test_동기화는_DB에서_바뀐_내용으로_기존_행을_고친다(sync, api):
    ls, store = sync
    r = add(store, day=WED, user="U1")
    ls.upsert(r.id)
    store.update_report(r.id, "DB에서 바뀜", at("2026-10-29T09:15"))
    add(store, day=D("2026-10-27"), user="U2")

    ls.sync(D("2026-10-27"), WED)

    upd = api.last("slackLists.items.update")
    assert (
        field(upd, "content", "cells")["rich_text"][0]["elements"][0]["elements"][0]["text"]
        == "DB에서 바뀜"
    )


def test_수정한_적_없는_보고의_수정_시각_칸은_비어_있다(sync, api):
    ls, store = sync
    ls.upsert(add(store).id)
    item = api.last("slackLists.items.create")
    # 빈 글자를 보내지 않고 칸을 비워 둔다
    assert all(f["column_id"] != "Col_updated_at" for f in item["initial_fields"])


# ---------------------------------------------------------------- 달력 경계 (한국 공휴일)


@pytest.mark.parametrize(
    "day",
    [
        "2026-03-02",
        "2026-05-25",
        "2026-06-03",
        "2026-08-17",
        "2026-10-05",
        "2026-10-09",
        "2026-09-25",
    ],
)
def test_대체공휴일_임시공휴일_금요일_월요일_공휴일은_쉰다(day):
    assert not WorkCalendar().is_workday(D(day))


def test_월요일_공휴일_다음_날_요약은_금요일부터_연휴를_모두_다룬다():
    # 10/5(월) 대체공휴일: 10/6(화) 요약 = 10/2(금) ~ 10/5(월)
    assert WorkCalendar().summary_range(D("2026-10-06")) == [
        D("2026-10-02"),
        D("2026-10-03"),
        D("2026-10-04"),
        D("2026-10-05"),
    ]


def test_금요일_공휴일_뒤_요약은_목요일부터_다룬다():
    # 10/9(금) 한글날: 10/12(월) 요약 = 10/8(목) ~ 10/11(일)
    assert WorkCalendar().summary_range(D("2026-10-12")) == [
        D("2026-10-08"),
        D("2026-10-09"),
        D("2026-10-10"),
        D("2026-10-11"),
    ]


def test_추석_연휴를_건너뛰고_수정_가능_기간을_센다():
    # 9/24~26 추석, 9/27 일: 9/23(수) 보고 -> 9/28(1) 29(2) 30(3) 10/1(4) 10/2(5)
    assert WorkCalendar().edit_last_day(D("2026-09-23"), 5) == D("2026-10-02")


def test_연말_연시와_윤년_경계에서_수정_가능_기간을_센다():
    cal = WorkCalendar()
    assert cal.edit_last_day(D("2026-12-30"), 5) == D("2027-01-07")
    assert cal.edit_last_day(D("2028-02-28"), 2) == D("2028-03-02")  # 2/29(1) 3/1 삼일절 3/2(2)


def test_수정_가능_기간_마지막_날_자정_직전과_직후():
    cal = WorkCalendar()
    last = D("2026-11-02")
    assert cal.is_editable(
        D("2026-10-26"), datetime(2026, 11, 2, 23, 59, 59, 999999, tzinfo=KST), 5
    )
    assert not cal.is_editable(D("2026-10-26"), datetime(2026, 11, 3, 0, 0, 0, tzinfo=KST), 5)
    assert cal.edit_last_day(D("2026-10-26"), 5) == last


# ---------------------------------------------------------------- app.py 연결


class FakeApp:
    def __init__(self):
        self.handlers = {}

    def _reg(self, kind):
        def deco_for(key):
            def deco(fn):
                self.handlers.setdefault((kind, key), []).append(fn)
                return fn

            return deco

        return deco_for

    def __getattr__(self, name):
        if name in ("event", "action", "view"):
            return self._reg(name)
        raise AttributeError(name)


class SpyBot:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a, k))


@pytest.fixture
def wired():
    from daily_report.app import _register

    app, bot = FakeApp(), SpyBot()
    _register(app, bot)
    return app, bot


def test_모든_액션과_모달_콜백이_등록된다(wired):
    app, _ = wired
    expected_actions = {
        views.OPEN_REPORT,
        views.WRITE_REPORT,
        views.EDIT_REPORT,
        views.DELETE_REPORT,
        views.OPEN_LEAVE,
        views.OPEN_LEAVE_CANCEL,
        views.ADMIN_EXCLUSIONS,
        views.ADMIN_CALENDAR,
        views.ADMIN_SYNC,
        views.ADMIN_LEAVE,
        views.ADMIN_LEAVE_CANCEL,
        views.ADMIN_LEAVE_STATUS,
    }
    expected_views = {
        views.REPORT_SUBMIT,
        views.LEAVE_SUBMIT,
        views.EXCLUSIONS_SUBMIT,
        views.CALENDAR_SUBMIT,
        views.SYNC_SUBMIT,
    }
    assert {k for kind, k in app.handlers if kind == "action"} == expected_actions
    assert {k for kind, k in app.handlers if kind == "view"} == expected_views
    assert {k for kind, k in app.handlers if kind == "event"} == {
        "app_home_opened",
        "member_joined_channel",
        "member_left_channel",
    }


BODY = {"user": {"id": "U1"}, "trigger_id": "T"}


def test_버튼_value가_봇까지_전달된다(wired):
    app, bot = wired
    app.handlers[("action", views.OPEN_REPORT)][0](lambda: None, BODY, {"value": "2026-10-27"})
    app.handlers[("action", views.DELETE_REPORT)][0](lambda: None, BODY, {"value": "7"})
    assert ("open_report", ("U1", "T", "2026-10-27"), {}) in bot.calls
    assert ("delete_report", ("U1", "7"), {}) in bot.calls


def test_연차_취소_버튼은_취소_모달을_연다(wired):
    app, bot = wired
    app.handlers[("action", views.OPEN_LEAVE_CANCEL)][0](lambda: None, BODY)
    assert bot.calls[-1][2] == {"register": False, "for_other": False}


def test_모달_오류는_response_action_errors로_ack한다(wired):
    app, bot = wired
    acks = []
    captured = {}

    class Bot2(SpyBot):
        def submit_report(self, uid, view, ack=None):
            captured["ack"] = ack

    app2 = FakeApp()
    from daily_report.app import _register

    _register(app2, Bot2())
    app2.handlers[("view", views.REPORT_SUBMIT)][0](lambda **kw: acks.append(kw), BODY, {})
    captured["ack"]({"date": "오류"})
    assert acks == [{"response_action": "errors", "errors": {"date": "오류"}}]


def test_앱_홈_탭이_아닌_이벤트는_무시한다(wired):
    app, bot = wired
    app.handlers[("event", "app_home_opened")][0]({"tab": "messages", "user": "U1"})
    assert bot.calls == []
    app.handlers[("event", "app_home_opened")][0]({"tab": "home", "user": "U1"})
    assert bot.calls == [("home_opened", ("U1",), {})]


def test_LLM_클라이언트는_설정한_주소를_쓴다(tmp_path):
    from daily_report.app import llm_client
    from daily_report.config import Config
    from tests.conftest import ENV

    cfg = Config.from_env({**ENV, "DB_PATH": str(tmp_path / "r.db")})
    assert str(llm_client(cfg).base_url).rstrip("/") == "http://llm/v1"


# ---------------------------------------------------------------- 화면 <-> 핸들러 계약


def test_각_모달의_callback_id는_앱에_등록된_제출_핸들러와_일치한다(wired):
    app, _ = wired
    registered = {k for kind, k in app.handlers if kind == "view"}
    modals = {
        views.REPORT_SUBMIT: views.report_modal(WED),
        views.LEAVE_SUBMIT: views.leave_modal(True, False),
        views.EXCLUSIONS_SUBMIT: views.exclusions_modal([]),
        views.CALENDAR_SUBMIT: views.calendar_modal(),
        views.SYNC_SUBMIT: views.sync_modal(),
    }
    for expected, modal in modals.items():
        assert modal["callback_id"] == expected and expected in registered


def test_연차_모달의_종류_값은_DB_상수와_같다():
    kind = next(b for b in views.leave_modal(True, False)["blocks"] if b.get("block_id") == "kind")
    assert {o["value"] for o in kind["element"]["options"]} == {ANNUAL, HALF}


# ---------------------------------------------------------------- .env 설정이 동작에 전달되는지


def test_REQUEST_TIME_설정을_바꾸면_그_시각에_요청한다(setup):
    jobs, _, slack, *_ = setup("2026-10-28T16:00", REQUEST_TIME="16:00")
    jobs.request(at("2026-10-28T16:00"))
    assert len(dms(slack)) == 3


def test_SUMMARY_DEADLINE_설정을_바꾸면_그_시한까지_요약한다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T19:00", SUMMARY_DEADLINE="20:00")
    store.save_targets(WED, {"U1"})
    jobs.summary(at("2026-10-29T19:00"))
    assert (
        not any("놓쳤어요" in p["text"] for p in admin_posts(slack))
        and len(admin_posts(slack)) == 1
    )


def test_EDIT_WINDOW_WORKDAYS_설정이_보고_제출에_적용된다(make_bot):
    bot, _, store, *_ = make_bot(now="2026-10-28T18:00", EDIT_WINDOW_WORKDAYS="1")
    # 10/26(월)의 기간은 10/27까지 -> 10/28에는 낼 수 없다
    errors = bot.submit_report("U1", view({"existing_id": None}, date="2026-10-26", content="x"))
    assert list(errors) == ["date"] and "수정 가능 기간" in errors["date"]


def test_SUMMARY_TIME_설정이_앱_홈_기본_날짜에_적용된다(make_bot):
    bot, slack, *_ = make_bot(now="2026-10-28T11:00", SUMMARY_TIME="12:00")
    bot.open_report("U1", "trig", "")  # 12:00 전 -> 직전 근무일
    assert slack.last("views_open")["view"]["blocks"][0]["element"]["initial_date"] == "2026-10-27"


def test_출근일로_지정한_토요일에는_평일처럼_요청한다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-31T17:30")
    store.set_override(SAT, "work")
    jobs.request(at("2026-10-31T17:30"))
    assert len(dms(slack)) == 3 and store.get_targets(SAT) is not None


def test_휴무일로_바꾼_날에는_요청하지_않는다(setup):
    jobs, bot, slack, store, *_ = setup("2026-10-28T17:30")
    bot.submit_calendar("UADMIN", view(day="2026-10-28", kind="off"))
    slack.calls.clear()  # 관리자 확인 DM은 빼고 본다
    jobs.request(at("2026-10-28T17:30"))
    assert slack.of("chat_postMessage") == []


# ---------------------------------------------------------------- HEAD(4e92d58) 신규 기능 보강


def test_설치한_날의_보고도_다음_날_재알림_대상이다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T09:00")
    store.mark_job_run("installed", WED, at("2026-10-28T09:00"))
    store.save_targets(WED, {"U1"})

    jobs.reminder(at("2026-10-29T09:00"))

    assert set(dms(slack)) == {"U1"}


def test_설치일_이전_재알림은_보내지_않고_완료로_기록한다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T09:00")
    store.mark_job_run("installed", THU, at("2026-10-29T08:00"))

    jobs.reminder(at("2026-10-29T09:00"))

    assert slack.of("chat_postMessage") == [] and store.has_job_run("reminder", WED)


def test_요청과_재알림_DM_문구는_각각_자기_문구다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    jobs.request(at("2026-10-28T17:30"))
    assert slack.of("chat_postMessage")[0]["text"] == views.request_text(WED)

    jobs2, _, slack2, store2, *_ = setup("2026-10-29T09:00")
    store2.save_targets(WED, {"U1"})
    jobs2.reminder(at("2026-10-29T09:00"))
    assert slack2.of("chat_postMessage")[0]["text"] == views.reminder_text(WED, time(10, 0))


def test_영구_오류를_받은_사람은_다른_사람의_일시_오류로_재시도할_때_다시_보내지_않는다(setup):
    from slack_sdk.errors import SlackApiError

    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    real = slack.chat_postMessage
    attempts = []

    def flaky(**kw):
        attempts.append(kw["channel"])
        if kw["channel"] == "U1":
            raise SlackApiError("x", {"ok": False, "error": "user_disabled"})
        if kw["channel"] == "U2" and attempts.count("U2") == 1:
            raise SlackApiError("x", {"ok": False, "error": "ratelimited"})
        return real(**kw)

    slack.chat_postMessage = flaky
    jobs.request(at("2026-10-28T17:30"))
    jobs.request(at("2026-10-28T17:31"))

    assert attempts.count("U1") == 1


def test_리스트_정합성_점검은_최근_45일을_대상으로_한다(setup):
    jobs, _, _, _, lists, _ = setup("2026-10-30T09:00")
    jobs.monthly_list(at("2026-10-30T09:00"))
    assert lists.reconciled == [D("2026-09-15")]


def test_이미_요약한_날은_놓쳤다고_하지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-30T10:00")
    store.mark_job_run("installed", D("2026-10-19"), at("2026-10-19T09:00"))
    for day in [
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
        "2026-10-23",
        "2026-10-26",
        "2026-10-27",
        "2026-10-28",
        "2026-10-29",
    ]:
        store.mark_job_run("summary", D(day), at(f"{day}T10:00"))
    store.save_targets(THU, {"U1"})

    jobs.summary(at("2026-10-30T10:00"))

    assert not any("놓쳤어요" in p["text"] for p in admin_posts(slack))


def test_사용자_목록을_새로_읽지_못하면_모르는_사람으로_본다(make_bot):
    bot, slack, *_ = make_bot()
    bot.directory.members()

    def boom(**kw):
        raise RuntimeError("down")

    slack.users_list = boom
    assert bot.directory.knows("UNEW") is False


def test_동기화에서_실패한_보고는_upserted에서_빠지고_failed로_센다(sync, api):
    ls, store = sync
    add(store, day=WED, user="U1")
    add(store, day=D("2026-10-27"), user="U2")
    ls.ensure_list("2026-10")
    api.fail["slackLists.items.create"] = "boom"

    result = ls.sync(D("2026-10-27"), WED)

    assert result == {"upserted": 0, "deleted": 0, "failed": 2}


def test_일시_오류로_행_삭제가_실패하면_삼키지_않고_매핑을_남긴다(sync, api):
    ls, store = sync
    r = add(store)
    ls.upsert(r.id)
    store.delete_report(r.id)
    api.fail["slackLists.items.delete"] = "ratelimited"

    with pytest.raises(ListError):
        ls.remove(r.id)

    assert store.get_list_item(r.id) is not None


def test_정합성_점검은_기준일_당일_보고도_포함한다():
    store = Store(":memory:")
    rid = store.add_report("U1", D("2026-09-15"), "x", at("2026-09-15T18:00"))
    assert store.reports_without_list_item(D("2026-09-15")) == [rid]
