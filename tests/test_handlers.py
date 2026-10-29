import json
from datetime import date

import pytest

from daily_report import views
from daily_report.db import ANNUAL, HALF
from tests.conftest import at, user

WED = date(2026, 10, 28)
TUE = date(2026, 10, 27)


def state(**blocks):
    """view.state.values 흉내. 값 종류는 블록 ID로 고른다."""
    values = {}
    for block_id, v in blocks.items():
        if block_id in ("date", "start", "end", "day"):
            values[block_id] = {block_id: {"type": "datepicker", "selected_date": v}}
        elif block_id == "content":
            values[block_id] = {"content": {"type": "plain_text_input", "value": v}}
        elif block_id == "kind":
            values[block_id] = {"kind": {"type": "radio_buttons", "selected_option": {"value": v}}}
        elif block_id == "user":
            values[block_id] = {"user": {"type": "users_select", "selected_user": v}}
        elif block_id == "users":
            values[block_id] = {"users": {"type": "multi_users_select", "selected_users": v}}
    return values


def view(meta=None, **blocks):
    return {"private_metadata": json.dumps(meta or {}), "state": {"values": state(**blocks)}}


def home_ids(slack):
    blocks = slack.last("views_publish")["view"]["blocks"]
    ids = []
    for b in blocks:
        ids += [e.get("action_id") for e in b.get("elements", [])]
    return ids, json.dumps(blocks, ensure_ascii=False)


# 앱 홈


def test_앱_홈을_열면_보고_대상자에게_보고_영역을_보여준다(make_bot):
    bot, slack, *_ = make_bot()

    bot.home_opened("U1")

    ids, _ = home_ids(slack)
    assert slack.last("views_publish")["user_id"] == "U1"
    assert views.OPEN_REPORT in ids and views.ADMIN_SYNC not in ids


def test_제외_명단에_있는_관리자는_안내와_관리_메뉴를_본다(make_bot):
    bot, slack, store, *_ = make_bot()
    store.add_exclusion("UADMIN")

    bot.home_opened("UADMIN")

    ids, text = home_ids(slack)
    assert "보고 대상이 아니에요" in text
    assert views.ADMIN_SYNC in ids and views.OPEN_REPORT not in ids


def test_앱_홈_날짜_목록은_대상이었던_날과_상태를_보여준다(make_bot):
    bot, slack, store, *_ = make_bot(now="2026-10-28T18:00")
    store.save_targets(date(2026, 10, 22), {"U2"})  # U1은 10/22 대상 아님
    store.add_report("U1", TUE, "화요일 작업", at("2026-10-27T18:00"))
    store.add_leaves("U1", [date(2026, 10, 26)], ANNUAL)
    store.add_leaves("U1", [date(2026, 10, 23)], HALF)
    store.add_report("U1", date(2026, 10, 25), "일요일 근무", at("2026-10-25T15:00"))

    rows = bot.home_rows("U1")

    got = [(r.day.isoformat(), r.status) for r in rows]
    assert got == [
        ("2026-10-28", "missing"),  # 오늘(목록 없음 → 지금 대상자)
        ("2026-10-27", "submitted"),
        ("2026-10-26", "annual"),
        ("2026-10-25", "submitted"),  # 쉬는 날은 보고가 있을 때만
        ("2026-10-23", "half"),
        # 10/22는 대상이 아니었던 날이라 빠진다
        ("2026-10-21", "missing"),  # 10/21의 기간은 10/28까지
    ]
    assert rows[1].last_day == date(2026, 11, 3)


# 보고 모달


def test_DM_버튼은_그_날짜로_모달을_연다(make_bot):
    bot, slack, *_ = make_bot(now="2026-10-29T12:00")

    bot.open_report("U1", "trig", "2026-10-27")

    v = slack.last("views_open")["view"]
    assert slack.last("views_open")["trigger_id"] == "trig"
    assert v["blocks"][0]["element"]["initial_date"] == "2026-10-27"


def test_앱_홈_보고_버튼은_기본_날짜_규칙을_쓴다(make_bot):
    bot, slack, *_ = make_bot(now="2026-10-28T09:30")

    bot.open_report("U1", "trig", "")

    assert slack.last("views_open")["view"]["blocks"][0]["element"]["initial_date"] == "2026-10-27"


def test_그_날짜에_보고가_있으면_불러와_수정_창으로_연다(make_bot):
    bot, slack, store, *_ = make_bot()
    rid = store.add_report("U1", WED, "원래", at("2026-10-28T17:00"))

    bot.open_report("U1", "trig", "2026-10-28")

    v = slack.last("views_open")["view"]
    assert json.loads(v["private_metadata"]) == {"existing_id": rid}
    assert v["blocks"][1]["element"]["initial_value"] == "원래"


def test_보고를_제출하면_저장하고_리스트와_앱_홈에_반영한다(make_bot):
    bot, slack, store, lists, _ = make_bot()

    errors = bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="작업"))

    assert errors is None
    assert store.get_report("U1", WED).content == "작업"
    assert lists.upserted == [store.get_report("U1", WED).id]
    assert slack.last("views_publish")["user_id"] == "U1"


def test_규칙에_어긋나면_모달_칸에_오류를_돌려준다(make_bot):
    bot, _, store, lists, _ = make_bot()

    errors = bot.submit_report("U1", view({"existing_id": None}, date="2026-10-29", content="x"))

    assert errors == {"date": "미래 날짜는 고를 수 없어요."}
    assert store.get_report("U1", date(2026, 10, 29)) is None
    assert lists.upserted == []


def test_리스트_기록이_실패해도_보고는_저장된다(make_bot):
    bot, _, store, lists, _ = make_bot()
    lists.fail = True

    errors = bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="x"))

    assert errors is None
    assert store.get_report("U1", WED) is not None


def test_수정_버튼은_본인_보고만_연다(make_bot):
    bot, slack, store, *_ = make_bot()
    rid = store.add_report("U2", WED, "남의 보고", at("2026-10-28T17:00"))

    bot.edit_report("U1", "trig", str(rid))

    assert slack.of("views_open") == []
    assert "본인" in slack.last("chat_postMessage")["text"]


def test_삭제하면_리스트에서도_지우고_앱_홈을_다시_그린다(make_bot):
    bot, slack, store, lists, _ = make_bot()
    rid = store.add_report("U1", WED, "x", at("2026-10-28T17:00"))

    bot.delete_report("U1", str(rid))

    assert store.get_report_by_id(rid) is None
    assert lists.removed == [rid]
    assert slack.last("views_publish")["user_id"] == "U1"


# 연차·반차


def test_본인_연차를_등록한다(make_bot):
    bot, _, store, *_ = make_bot()
    meta = {"register": True, "for_other": False}

    errors = bot.submit_leave("U1", view(meta, kind="annual", start="2026-11-02", end="2026-11-03"))

    assert errors is None
    assert store.get_leave("U1", date(2026, 11, 3)) == ANNUAL


def test_연차_규칙에_어긋나면_해당_칸에_오류(make_bot):
    bot, *_ = make_bot()
    meta = {"register": True, "for_other": False}

    errors = bot.submit_leave("U1", view(meta, kind="annual", start="2026-10-31"))

    assert list(errors) == ["start"]


def test_다른_사람_연차는_관리자만_다룬다(make_bot):
    bot, slack, store, *_ = make_bot()
    meta = {"register": True, "for_other": True}

    bot.open_leave("U1", "trig", register=True, for_other=True)
    assert slack.of("views_open") == []

    errors = bot.submit_leave("UADMIN", view(meta, user="U2", kind="half", start="2026-11-02"))
    assert errors is None
    assert store.get_leave("U2", date(2026, 11, 2)) == HALF

    errors = bot.submit_leave("U1", view(meta, user="U2", kind="annual", start="2026-11-03"))
    assert errors == {"user": "관리자만 할 수 있어요."}


def test_연차를_취소한다(make_bot):
    bot, _, store, *_ = make_bot()
    store.add_leaves("U1", [date(2026, 11, 2)], ANNUAL)

    errors = bot.submit_leave(
        "U1", view({"register": False, "for_other": False}, start="2026-11-02")
    )

    assert errors is None and store.get_leave("U1", date(2026, 11, 2)) is None


# 관리 메뉴


def test_관리자가_아니면_관리_메뉴를_열_수_없다(make_bot):
    bot, slack, *_ = make_bot()

    bot.open_admin("U1", "trig", views.ADMIN_EXCLUSIONS)

    assert slack.of("views_open") == []
    assert "관리자만" in slack.last("chat_postMessage")["text"]


def test_제외_명단을_바꾼다(make_bot):
    bot, slack, store, *_ = make_bot()
    store.add_exclusion("U1")

    bot.open_admin("UADMIN", "trig", views.ADMIN_EXCLUSIONS)
    initial = slack.last("views_open")["view"]["blocks"][0]["element"]["initial_users"]
    errors = bot.submit_exclusions("UADMIN", view(users=["U2"]))

    assert initial == ["U1"]
    assert errors is None and store.exclusions() == {"U2"}


def test_휴무일을_추가하면_그날_대상자_목록을_지운다(make_bot):
    bot, _, store, *_ = make_bot(now="2026-10-28T18:00")
    store.save_targets(WED, {"U1"})

    errors = bot.submit_calendar("UADMIN", view(day="2026-10-28", kind="off"))

    assert errors is None
    assert store.overrides() == {WED: "off"} and store.get_targets(WED) is None


def test_출근일_지정_규칙에_어긋나면_날짜_칸에_오류(make_bot):
    bot, *_ = make_bot(now="2026-10-31T08:00")

    errors = bot.submit_calendar("UADMIN", view(day="2026-10-31", kind="work"))

    assert list(errors) == ["day"]


def test_동기화_기간이_거꾸로면_오류(make_bot):
    bot, _, _, lists, _ = make_bot()

    assert bot.validate_sync("UADMIN", view(start="2026-10-28", end="2026-10-01")) == {
        "end": "종료일이 시작일보다 빨라요."
    }
    assert lists.synced == []


def test_동기화를_실행하고_결과를_알린다(make_bot):
    bot, slack, _, lists, _ = make_bot()

    bot.run_sync("UADMIN", view(start="2026-10-01", end="2026-10-28"))

    assert lists.synced == [(date(2026, 10, 1), WED)]
    assert "1건" in slack.last("chat_postMessage")["text"]


def test_관리자_채널에_들어오고_나간_사람의_관리_메뉴_권한이_바뀐다(make_bot):
    bot, slack, *_ = make_bot()

    slack.admins.append("U1")
    bot.member_joined("CADMIN", "U1")
    bot.open_admin("U1", "trig", views.ADMIN_SYNC)
    assert len(slack.of("views_open")) == 1

    slack.admins.remove("U1")
    bot.member_left("CADMIN", "U1")
    bot.open_admin("U1", "trig", views.ADMIN_SYNC)
    assert len(slack.of("views_open")) == 1


def test_시험_모드면_시험_대상자만_대상이다(make_bot):
    bot, slack, *_ = make_bot(TEST_TARGET_USERS="U2")

    bot.home_opened("U1")
    _, text = home_ids(slack)

    assert "보고 대상이 아니에요" in text
    assert bot.current_targets() == {"U2"}


def test_게스트에게는_안내만_보인다(make_bot):
    bot, slack, *_ = make_bot(users=[user("U1", is_restricted=True), user("UADMIN")])

    bot.home_opened("U1")

    assert "보고 대상이 아니에요" in home_ids(slack)[1]


def test_제출은_응답을_먼저_보내고_리스트와_앱_홈을_반영한다(make_bot):
    bot, slack, _, lists, _ = make_bot()
    order = []
    lists.upsert = lambda r: order.append("list")
    slack.views_publish = lambda **kw: order.append("home")

    bot.submit_report(
        "U1",
        view({"existing_id": None}, date="2026-10-28", content="x"),
        ack=lambda errors=None: order.append(("ack", errors)),
    )

    assert order == [("ack", None), "home", "list"]


def test_오류도_응답으로_보낸다(make_bot):
    bot, *_ = make_bot()
    acked = []

    bot.submit_report(
        "U1",
        view({"existing_id": None}, date="2026-10-29", content="x"),
        ack=lambda errors=None: acked.append(errors),
    )

    assert acked == [{"date": "미래 날짜는 고를 수 없어요."}]


def test_연차_휴무일_제외_명단_제출도_응답을_보낸다(make_bot):
    bot, *_ = make_bot()
    acked = []
    ack = lambda errors=None: acked.append(errors)  # noqa: E731

    bot.submit_leave("U1", view({"register": True}, kind="annual", start="2026-11-02"), ack=ack)
    bot.submit_calendar("UADMIN", view(day="2026-11-07", kind="work"), ack=ack)
    bot.submit_exclusions("UADMIN", view(users=[]), ack=ack)

    assert acked == [None, None, None]


# 리뷰 반영: 실패 처리와 관리자 확인


def test_리스트_기록이_예상_못한_오류로_실패해도_앱_홈을_다시_그린다(make_bot):
    bot, slack, store, lists, _ = make_bot()

    def boom(report):
        raise RuntimeError("slack down")

    lists.upsert = boom

    assert (
        bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="x")) is None
    )
    assert store.get_report("U1", WED) is not None
    assert slack.last("views_publish")["user_id"] == "U1"


def test_리스트_삭제가_실패해도_앱_홈을_다시_그린다(make_bot):
    bot, slack, store, lists, _ = make_bot()
    rid = store.add_report("U1", WED, "x", at("2026-10-28T17:00"))

    def boom(report_id):
        raise RuntimeError("slack down")

    lists.remove = boom
    bot.delete_report("U1", str(rid))

    assert store.get_report_by_id(rid) is None
    assert slack.last("views_publish")["user_id"] == "U1"


def test_동기화가_예상_못한_오류로_실패해도_관리자에게_알린다(make_bot):
    bot, slack, _, lists, _ = make_bot()

    def boom(start, end):
        raise RuntimeError("ratelimited")

    lists.sync = boom
    bot.run_sync("UADMIN", view(start="2026-10-01", end="2026-10-28"))

    assert "실패" in slack.last("chat_postMessage")["text"]


def test_관리자_확인이_실패해도_앱_홈은_관리_메뉴_없이_열린다(make_bot):
    bot, slack, *_ = make_bot()

    def boom(**kw):
        raise RuntimeError("not_in_channel")

    slack.conversations_members = boom
    bot.home_opened("U1")

    ids, _ = home_ids(slack)
    assert views.OPEN_REPORT in ids and views.ADMIN_SYNC not in ids


@pytest.mark.parametrize(
    "call",
    [
        lambda bot: bot.submit_exclusions("U1", view(users=["U2"])),
        lambda bot: bot.submit_calendar("U1", view(day="2026-11-07", kind="work")),
        lambda bot: bot.validate_sync("U1", view(start="2026-10-01", end="2026-10-28")),
    ],
)
def test_관리_메뉴_제출은_관리자가_아니면_거부한다(make_bot, call):
    bot, _, store, *_ = make_bot()

    errors = call(bot)

    assert "관리자만" in next(iter(errors.values()))
    assert store.exclusions() == set() and store.overrides() == {}


def test_동기화_실행도_스스로_관리자를_확인한다(make_bot):
    bot, slack, _, lists, _ = make_bot()

    bot.run_sync("U1", view(start="2026-10-01", end="2026-10-28"))

    assert lists.synced == []
    assert "관리자만" in slack.last("chat_postMessage")["text"]


def test_관리_메뉴는_실행할_때_관리자_목록을_새로_확인한다(make_bot):
    bot, slack, *_ = make_bot()
    bot.home_opened("UADMIN")  # 관리자 목록을 읽어 둠
    slack.admins = []  # 그 뒤 채널에서 내보내짐(이벤트 누락)

    bot.open_admin("UADMIN", "trig", views.ADMIN_SYNC)

    assert slack.of("views_open") == []


def test_관리자는_다른_사람_연차_취소_창을_연다(make_bot):
    bot, slack, *_ = make_bot()

    bot.open_admin("UADMIN", "trig", views.ADMIN_LEAVE_CANCEL)

    meta = json.loads(slack.last("views_open")["view"]["private_metadata"])
    assert meta == {"register": False, "for_other": True}


def test_다른_사람_연차의_대상은_보고_대상자여야_한다(make_bot):
    bot, _, store, *_ = make_bot()
    store.add_exclusion("U2")
    meta = {"register": True, "for_other": True}

    errors = bot.submit_leave("UADMIN", view(meta, user="U2", kind="annual", start="2026-11-02"))

    assert errors["user"].startswith("보고 대상자가 아니에요.")


def test_관리자는_제외_명단에_있는_사람의_연차도_취소할_수_있다(make_bot):
    bot, _, store, *_ = make_bot()
    store.add_leaves("U2", [date(2026, 11, 2)], "annual")
    store.add_exclusion("U2")

    errors = bot.submit_leave(
        "UADMIN", view({"register": False, "for_other": True}, user="U2", start="2026-11-02")
    )

    assert errors is None and store.get_leave("U2", date(2026, 11, 2)) is None


def test_목록에_없는_사용자가_앱_홈을_열면_사용자_목록을_새로_읽는다(make_bot):
    bot, slack, *_ = make_bot()
    bot.home_opened("U1")
    slack.users.append(user("UNEW"))  # 캐시를 채운 뒤 입사

    bot.home_opened("UNEW")

    assert "보고 대상이 아니에요" not in home_ids(slack)[1]


def test_대상이_아니었던_날의_오류는_기준을_알려준다(make_bot):
    bot, _, store, *_ = make_bot()
    store.save_targets(TUE, {"U2"})

    errors = bot.submit_report("U1", view({"existing_id": None}, date="2026-10-27", content="x"))

    assert "보고 요청 때 대상자 목록" in errors["date"]


# 3차 리뷰 반영


def test_제출하면_앱_홈을_리스트_기록보다_먼저_다시_그린다(make_bot):
    bot, slack, _, lists, _ = make_bot()
    seen = []
    lists.upsert = lambda rid: seen.append(len(slack.of("views_publish")))

    bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="x"))

    assert seen == [1]


def test_삭제하면_앱_홈을_리스트_기록보다_먼저_다시_그린다(make_bot):
    bot, slack, store, lists, _ = make_bot()
    rid = store.add_report("U1", WED, "x", at("2026-10-28T17:00"))
    seen = []
    lists.remove = lambda r: seen.append(len(slack.of("views_publish")))

    bot.delete_report("U1", str(rid))

    assert seen == [1]


def test_창을_열지_못하면_다시_눌러_달라고_알린다(make_bot):
    bot, slack, *_ = make_bot()
    slack.fail["views_open"] = "expired_trigger_id"

    bot.open_report("U1", "trig", "2026-10-28")

    assert "다시" in slack.last("chat_postMessage")["text"]


def test_이미_지운_보고의_수정_버튼은_보고를_찾을_수_없다고_알린다(make_bot):
    bot, slack, *_ = make_bot()

    bot.edit_report("U1", "trig", "999")

    assert "찾을 수 없어요" in slack.last("chat_postMessage")["text"]


def test_동기화에_실패한_건이_있으면_건수를_알린다(make_bot):
    bot, slack, _, lists, _ = make_bot()
    lists.sync = lambda s, e: {"upserted": 2, "deleted": 0, "failed": 1}

    bot.run_sync("UADMIN", view(start="2026-10-01", end="2026-10-28"))

    text = slack.last("chat_postMessage")["text"]
    assert "실패 1건" in text and "10/1(목)" in text


def test_앱_홈을_다시_그리지_못해도_리스트에는_기록한다(make_bot):
    bot, slack, store, lists, _ = make_bot()
    slack.fail["views_publish"] = "internal_error"

    bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="x"))

    assert lists.upserted == [store.get_report("U1", WED).id]


def test_삭제한_뒤_앱_홈을_다시_그리지_못해도_리스트에서_지운다(make_bot):
    bot, slack, store, lists, _ = make_bot()
    rid = store.add_report("U1", WED, "x", at("2026-10-28T17:00"))
    slack.fail["views_publish"] = "internal_error"

    bot.delete_report("U1", str(rid))

    assert lists.removed == [rid]


# 처리 후 확인 DM (SPEC 결정)


def dm_to(slack, uid):
    return [kw["text"] for kw in slack.of("chat_postMessage") if kw["channel"] == uid]


def test_보고를_저장하면_확인_DM을_보낸다(make_bot):
    bot, slack, *_ = make_bot()

    bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="x"))

    assert dm_to(slack, "U1") == ["10/28(수) 보고를 저장했어요."]


def test_보고를_옮기면_옮긴_날짜를_알린다(make_bot):
    bot, slack, store, *_ = make_bot()
    rid = store.add_report("U1", TUE, "x", at("2026-10-27T17:00"))

    bot.submit_report("U1", view({"existing_id": rid}, date="2026-10-28", content="x"))

    assert dm_to(slack, "U1") == ["보고를 옮겼어요: 10/27(화) → 10/28(수)"]


def test_규칙에_어긋나_저장하지_않으면_확인_DM을_보내지_않는다(make_bot):
    bot, slack, *_ = make_bot()

    bot.submit_report("U1", view({"existing_id": None}, date="2026-10-29", content="x"))

    assert dm_to(slack, "U1") == []


def test_연차를_기간으로_등록하면_기간과_일수를_알린다(make_bot):
    bot, slack, *_ = make_bot()
    meta = {"register": True, "for_other": False}

    bot.submit_leave("U1", view(meta, kind="annual", start="2026-10-30", end="2026-11-03"))

    assert dm_to(slack, "U1") == ["연차를 등록했어요: 10/30(금)~11/3(화), 3일"]


def test_반차를_취소하면_알린다(make_bot):
    bot, slack, store, *_ = make_bot()
    store.add_leaves("U1", [date(2026, 11, 2)], HALF)

    bot.submit_leave("U1", view({"register": False, "for_other": False}, start="2026-11-02"))

    assert dm_to(slack, "U1") == ["연차·반차를 취소했어요: 11/2(월)"]


def test_다른_사람_연차를_등록하면_관리자에게_이름과_함께_알린다(make_bot):
    bot, slack, *_ = make_bot()
    meta = {"register": True, "for_other": True}

    bot.submit_leave("UADMIN", view(meta, user="U2", kind="half", start="2026-11-02"))

    assert dm_to(slack, "UADMIN") == ["u2님의 반차를 등록했어요: 11/2(월)"]
    assert dm_to(slack, "U2") == []  # 당사자에게는 보내지 않는다


def test_제외_명단을_저장하면_인원을_알린다(make_bot):
    bot, slack, *_ = make_bot()

    bot.submit_exclusions("UADMIN", view(users=["U1", "U2"]))

    assert dm_to(slack, "UADMIN") == ["제외 명단을 저장했어요(2명)."]


@pytest.mark.parametrize(
    "kind, text",
    [
        ("work", "출근일로 지정했어요: 11/7(토)"),
        ("off", "휴무일로 지정했어요: 11/7(토)"),
    ],
)
def test_휴무일_출근일을_바꾸면_알린다(make_bot, kind, text):
    bot, slack, *_ = make_bot()

    bot.submit_calendar("UADMIN", view(day="2026-11-07", kind=kind))

    assert dm_to(slack, "UADMIN") == [text]


def test_추가한_휴무일을_취소하면_알린다(make_bot):
    bot, slack, store, *_ = make_bot()
    store.set_override(date(2026, 11, 6), "off")

    bot.submit_calendar("UADMIN", view(day="2026-11-06", kind="remove_off"))

    assert dm_to(slack, "UADMIN") == ["추가한 휴무일을 취소했어요: 11/6(금)"]


def test_확인_DM이_실패해도_보고는_저장되고_리스트에_기록한다(make_bot):
    bot, slack, store, lists, _ = make_bot()
    slack.fail["chat_postMessage"] = "internal_error"

    errors = bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="x"))

    assert errors is None and lists.upserted == [store.get_report("U1", WED).id]


def test_출근일_지정을_취소하면_알린다(make_bot):
    bot, slack, store, *_ = make_bot()
    store.set_override(date(2026, 11, 7), "work")

    errors = bot.submit_calendar("UADMIN", view(day="2026-11-07", kind="remove_work"))

    assert errors is None and store.overrides() == {}
    assert dm_to(slack, "UADMIN") == ["출근일 지정을 취소했어요: 11/7(토)"]


def test_앱_홈에_본인의_예정된_연차를_보여준다(make_bot):
    bot, slack, store, *_ = make_bot()
    store.add_leaves("U1", [date(2026, 11, 2)], ANNUAL)

    bot.home_opened("U1")

    assert "예정된 연차·반차: 11/2(월) 연차" in home_ids(slack)[1]


def test_다른_사람_연차를_취소하면_관리자에게만_알리고_목록에_없으면_멘션으로_쓴다(make_bot):
    bot, slack, store, *_ = make_bot()
    store.add_leaves("UGONE", [date(2026, 11, 2)], ANNUAL)
    meta = {"register": False, "for_other": True}

    bot.submit_leave("UADMIN", view(meta, user="UGONE", start="2026-11-02"))

    assert dm_to(slack, "UADMIN") == ["<@UGONE>님의 연차·반차를 취소했어요: 11/2(월)"]
    assert dm_to(slack, "UGONE") == []


def test_앱_홈을_못_그려도_확인_DM은_보내고_리스트_기록보다_먼저_보낸다(make_bot):
    bot, slack, _, lists, _ = make_bot()
    slack.fail["views_publish"] = "internal_error"
    seen = []
    lists.upsert = lambda rid: seen.append(dm_to(slack, "U1"))

    bot.submit_report("U1", view({"existing_id": None}, date="2026-10-28", content="x"))

    assert seen == [["10/28(수) 보고를 저장했어요."]]


def test_휴무일_창에는_오늘_이후_지정한_날만_보여준다(make_bot):
    bot, slack, store, *_ = make_bot(now="2026-10-29T10:00")
    store.set_override(date(2026, 10, 28), "off")  # 지난 날은 빼고
    store.set_override(date(2026, 10, 29), "off")
    store.set_override(date(2026, 10, 31), "work")

    bot.open_admin("UADMIN", "trig", views.ADMIN_CALENDAR)

    text = slack.last("views_open")["view"]["blocks"][0]["text"]["text"]
    assert "추가한 휴무일: 10/29(목)" in text and "10/28" not in text
    assert "지정한 출근일: 10/31(토)" in text


def test_휴무일을_지정하면_관리자_앱_홈에_바로_보인다(make_bot):
    bot, slack, *_ = make_bot(now="2026-10-29T10:00")

    bot.submit_calendar("UADMIN", view(day="2026-10-30", kind="off"))

    home = json.dumps(slack.last("views_publish")["view"], ensure_ascii=False)
    assert "추가한 휴무일: 10/30(금)" in home


def test_본인_연차_창을_열면_예정된_연차_반차가_보인다(make_bot):
    bot, slack, store, *_ = make_bot(now="2026-10-29T10:00")
    store.save_targets(date(2026, 10, 29), {"U1"})
    store.add_leaves("U1", [date(2026, 11, 2)], HALF)

    bot.open_leave("U1", "trig", register=False, for_other=False)

    text = slack.last("views_open")["view"]["blocks"][0]["text"]["text"]
    assert text == "예정된 연차·반차: 11/2(월) 반차"


def test_연차_현황은_오늘부터_4주_연차_반차를_이름순으로_보여준다(make_bot):
    bot, slack, store, *_ = make_bot(now="2026-10-29T10:00")
    store.add_leaves("U2", [date(2026, 11, 3)], HALF)
    store.add_leaves("U1", [date(2026, 10, 29)], ANNUAL)
    store.add_leaves("U1", [date(2026, 11, 26)], ANNUAL)  # 4주 뒤라 뺀다

    bot.open_admin("UADMIN", "trig", views.ADMIN_LEAVE_STATUS)

    text = json.dumps(slack.last("views_open")["view"], ensure_ascii=False)
    assert "10/29(목)~11/25(수)" in text
    assert "10/29(목) 연차" in text and "11/3(화) 반차" in text and "11/26" not in text
    assert text.index("*u1*") < text.index("*u2*")  # 이름순


def test_연차_현황은_관리자만_연다(make_bot):
    bot, slack, *_ = make_bot()

    bot.open_admin("U1", "trig", views.ADMIN_LEAVE_STATUS)

    assert slack.of("views_open") == []


def test_연차_현황은_지금_보고_대상자의_연차만_보여준다(make_bot):
    bot, slack, store, *_ = make_bot(now="2026-10-29T10:00")
    store.add_leaves("U1", [date(2026, 11, 3)], ANNUAL)
    store.add_leaves("U2", [date(2026, 11, 4)], ANNUAL)
    store.add_leaves("UGONE", [date(2026, 11, 5)], ANNUAL)  # 퇴사 등으로 목록에 없음
    store.add_exclusion("U2")

    bot.open_admin("UADMIN", "trig", views.ADMIN_LEAVE_STATUS)

    text = json.dumps(slack.last("views_open")["view"], ensure_ascii=False)
    assert "*u1*" in text and "u2" not in text and "UGONE" not in text


def test_연차_현황은_사용자_목록을_한_번만_읽는다(make_bot):
    bot, slack, store, *_ = make_bot(now="2026-10-29T10:00")
    store.add_leaves("U1", [date(2026, 11, 3)], ANNUAL)
    store.add_leaves("U2", [date(2026, 11, 4)], ANNUAL)

    bot.open_admin("UADMIN", "trig", views.ADMIN_LEAVE_STATUS)

    assert len(slack.of("users_list")) == 1


def test_본인_연차_창에는_오늘_연차도_보인다(make_bot):
    bot, slack, store, *_ = make_bot(now="2026-10-29T10:00")
    store.add_leaves("U1", [date(2026, 10, 29)], ANNUAL)

    bot.open_leave("U1", "trig", register=False, for_other=False)

    text = slack.last("views_open")["view"]["blocks"][0]["text"]["text"]
    assert text == "예정된 연차·반차: 10/29(목) 연차"
