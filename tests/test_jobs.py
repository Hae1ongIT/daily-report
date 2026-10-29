import json
from datetime import date

import pytest

from daily_report.db import ANNUAL, HALF
from daily_report.jobs import Jobs
from daily_report.summary import LlmSummary
from tests.conftest import at

WED = date(2026, 10, 28)
THU = date(2026, 10, 29)
FRI = date(2026, 10, 30)
DEFAULT = LlmSummary("팀 요약", [("u1", "작업")])


class FakeSummarizer:
    def __init__(self, result=DEFAULT):
        self.result = result
        self.calls = []

    def summarize(self, data):
        self.calls.append(data)
        return self.result


@pytest.fixture
def setup(make_bot):
    def make(now, result=DEFAULT, **kw):
        bot, slack, store, lists, clock = make_bot(now=now, **kw)
        summarizer = FakeSummarizer(result)
        return Jobs(bot, summarizer), bot, slack, store, lists, clock, summarizer

    return make


def dms(slack):
    return {kw["channel"]: kw for kw in slack.of("chat_postMessage") if kw["channel"] != "CADMIN"}


def admin_posts(slack):
    return [kw for kw in slack.of("chat_postMessage") if kw["channel"] == "CADMIN"]


# 17:30 보고 요청


def test_보고_요청은_아직_안_낸_대상자에게_보내고_대상자_목록을_저장한다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    store.add_report("U1", WED, "미리 냄", at("2026-10-28T15:00"))
    store.add_leaves("UADMIN", [WED], ANNUAL)

    jobs.request(at("2026-10-28T17:30"))

    assert set(dms(slack)) == {"U2"}
    assert store.get_targets(WED) == {"U1", "U2", "UADMIN"}  # 낸 사람·연차도 목록에 포함
    assert store.has_job_run("request", WED)


def test_반차인_사람에게도_요청을_보낸다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    store.add_leaves("U1", [WED], HALF)

    jobs.request(at("2026-10-28T17:30"))

    assert "U1" in dms(slack)


def test_요청은_하루에_한_번만_보낸다(setup):
    jobs, _, slack, *_ = setup("2026-10-28T17:30")

    jobs.request(at("2026-10-28T17:30"))
    jobs.request(at("2026-10-28T17:40"))

    sent = [kw["channel"] for kw in slack.of("chat_postMessage")]
    assert sorted(sent) == ["U1", "U2", "UADMIN"]


def test_쉬는_날에는_요청하지_않고_목록도_만들지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-31T17:30")

    jobs.request(at("2026-10-31T17:30"))

    assert slack.of("chat_postMessage") == []
    assert store.get_targets(date(2026, 10, 31)) is None


def test_요청_시한이_지나면_보내지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-28T21:01")

    jobs.request(at("2026-10-28T21:01"))

    assert slack.of("chat_postMessage") == []
    assert store.get_targets(WED) is None


def test_요청_시각_전에는_아무것도_하지_않는다(setup):
    jobs, _, slack, *_ = setup("2026-10-28T17:00")

    jobs.request(at("2026-10-28T17:00"))

    assert slack.of("chat_postMessage") == []


# 09:00 재알림


def test_재알림은_직전_근무일_미제출자에게_보낸다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T09:00")
    store.save_targets(WED, {"U1", "U2", "UADMIN"})
    store.add_report("U1", WED, "냄", at("2026-10-28T18:00"))

    jobs.reminder(at("2026-10-29T09:00"))

    assert set(dms(slack)) == {"U2", "UADMIN"}
    dm = dms(slack)["U2"]
    button = dm["blocks"][1]["elements"][0]
    assert button["value"] == "2026-10-28"


def test_재알림_날_연차인_사람에게는_보내지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T09:00")
    store.save_targets(WED, {"U1", "U2"})
    store.add_leaves("U1", [THU], ANNUAL)
    store.add_leaves("U2", [THU], HALF)  # 반차는 보낸다

    jobs.reminder(at("2026-10-29T09:00"))

    assert set(dms(slack)) == {"U2"}


def test_보고_날짜에_연차나_반차였던_사람은_미제출이_아니라서_보내지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T09:00")
    store.save_targets(WED, {"U1", "U2", "UADMIN"})
    store.add_leaves("U1", [WED], ANNUAL)
    store.add_leaves("U2", [WED], HALF)

    jobs.reminder(at("2026-10-29T09:00"))

    assert set(dms(slack)) == {"UADMIN"}


def test_17시30분_작업을_건너뛰었으면_재알림이_대상자_목록을_만든다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T09:00")

    jobs.reminder(at("2026-10-29T09:00"))

    assert store.get_targets(WED) == {"U1", "U2", "UADMIN"}
    assert len(dms(slack)) == 3


def test_월요일_재알림은_금요일_보고를_요청한다(setup):
    jobs, _, slack, store, *_ = setup("2026-11-02T09:00")
    store.save_targets(FRI, {"U1"})

    jobs.reminder(at("2026-11-02T09:00"))

    assert dms(slack)["U1"]["blocks"][1]["elements"][0]["value"] == "2026-10-30"


def test_재알림_시한은_요약_시각이다(setup):
    jobs, _, slack, *_ = setup("2026-10-29T10:01")

    jobs.reminder(at("2026-10-29T10:01"))

    assert slack.of("chat_postMessage") == []


# 10:00 요약


def test_요약을_관리자_채널에_올린다(setup):
    jobs, _, slack, store, _, _, summarizer = setup("2026-10-29T10:00")
    store.save_targets(WED, {"U1", "U2"})
    store.add_report("U1", WED, "작업", at("2026-10-28T18:00"))

    jobs.summary(at("2026-10-29T10:00"))

    post = admin_posts(slack)[0]
    body = json.dumps(post["blocks"], ensure_ascii=False)
    assert "제출 1/2" in body and "미제출: u2" in body and "팀 요약" in body
    assert "https://list/2026-10" in body
    assert summarizer.calls[0].workday == WED
    assert store.has_job_run("summary", THU)  # 요약 실행 기록은 요약하는 날 기준


def test_요약은_한_번만_올린다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T10:00")
    store.save_targets(WED, {"U1"})

    jobs.summary(at("2026-10-29T10:00"))
    jobs.summary(at("2026-10-29T10:05"))

    assert len(admin_posts(slack)) == 1


def test_LLM이_실패하면_실패_표시로_올린다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T10:00", result=None)
    store.save_targets(WED, {"U1"})

    jobs.summary(at("2026-10-29T10:00"))

    assert "LLM 요약 실패" in json.dumps(admin_posts(slack)[0]["blocks"], ensure_ascii=False)


def test_리스트_링크를_못_가져와도_요약은_올린다(setup):
    jobs, _, slack, store, lists, *_ = setup("2026-10-29T10:00")
    lists.fail_permalink = True
    store.save_targets(WED, {"U1"})

    jobs.summary(at("2026-10-29T10:00"))

    assert len(admin_posts(slack)) == 1


def test_요약_시한이_지나면_놓쳤다고_알린다(setup):
    jobs, _, slack, store, _, _, summarizer = setup("2026-10-29T18:01")

    jobs.summary(at("2026-10-29T18:01"))

    assert "요약을 놓쳤어요" in admin_posts(slack)[0]["text"]
    assert summarizer.calls == []
    jobs.summary(at("2026-10-29T18:30"))
    assert len(admin_posts(slack)) == 1


def test_쉬는_날에는_요약하지_않는다(setup):
    jobs, _, slack, *_ = setup("2026-10-31T10:00")

    jobs.summary(at("2026-10-31T10:00"))

    assert slack.of("chat_postMessage") == []


# 다시 켜졌을 때


def test_다시_켜지면_시한_안의_작업만_실행한다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T09:40")
    store.save_targets(WED, {"U1"})

    jobs.catch_up(at("2026-10-29T09:40"))

    assert set(dms(slack)) == {"U1"}  # 재알림은 시한(10:00) 안
    assert admin_posts(slack) == []  # 요약은 아직 시각 전


def test_그_달_리스트를_만든다(setup):
    jobs, _, _, _, lists, *_ = setup("2026-11-01T00:05")

    jobs.monthly_list(at("2026-11-01T00:05"))

    assert lists.ensured == ["2026-11"]


def test_시험_모드면_켜질_때_관리자_채널에_알린다(setup):
    jobs, _, slack, *_ = setup("2026-10-29T08:00", TEST_TARGET_USERS="U1,U2")

    jobs.announce_start()

    assert "시험 모드: 2명 대상" in admin_posts(slack)[0]["text"]


def test_시험_모드가_아니면_알리지_않는다(setup):
    jobs, _, slack, *_ = setup("2026-10-29T08:00")

    jobs.announce_start()

    assert admin_posts(slack) == []


# 리뷰 반영


def test_요청_DM_일부가_실패하면_다음_실행에서_실패한_사람에게만_다시_보낸다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    real = slack.chat_postMessage

    def flaky(**kw):
        if kw["channel"] == "U2":
            raise RuntimeError("ratelimited")
        return real(**kw)

    slack.chat_postMessage = flaky
    jobs.request(at("2026-10-28T17:30"))
    assert not store.has_job_run("request", WED)

    slack.chat_postMessage = real
    jobs.request(at("2026-10-28T17:31"))

    sent = [kw["channel"] for kw in slack.of("chat_postMessage")]
    assert sorted(sent) == ["U1", "U2", "UADMIN"]  # U1, UADMIN은 한 번씩만
    assert store.has_job_run("request", WED)


def test_요청이_중간에_끊겨도_이미_받은_사람에게는_다시_보내지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    store.record_dm("request", WED, "U1", at("2026-10-28T17:30"))  # 끊기기 전 보낸 기록

    jobs.request(at("2026-10-28T17:32"))

    assert "U1" not in dms(slack)


def test_요청_대상자_목록은_사용자_목록을_새로_읽어_만든다(setup):
    jobs, _, slack, *_ = setup("2026-10-28T17:30")
    jobs.bot.directory.members()  # 캐시를 채워 둠

    jobs.request(at("2026-10-28T17:30"))

    assert len(slack.of("users_list")) == 2


def test_요약_게시가_실패하면_다음_실행에서_LLM을_다시_부르지_않고_게시만_다시_한다(setup):
    jobs, _, slack, store, _, _, summarizer = setup("2026-10-29T10:00")
    store.save_targets(WED, {"U1"})
    real = slack.chat_postMessage

    def boom(**kw):
        raise RuntimeError("not_in_channel")

    slack.chat_postMessage = boom
    with pytest.raises(RuntimeError):
        jobs.summary(at("2026-10-29T10:00"))
    slack.chat_postMessage = real
    jobs.summary(at("2026-10-29T10:01"))

    assert len(summarizer.calls) == 1
    assert len(admin_posts(slack)) == 1


def test_리스트_링크_조회가_어떤_오류로_실패해도_요약은_올린다(setup):
    jobs, _, slack, store, lists, *_ = setup("2026-10-29T10:00")
    store.save_targets(WED, {"U1"})

    def boom(day):
        raise RuntimeError("slack down")

    lists.permalink = boom
    jobs.summary(at("2026-10-29T10:00"))

    assert len(admin_posts(slack)) == 1


def test_며칠_꺼져_있었으면_놓친_요약을_알린다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-30T10:00")
    store.mark_job_run("installed", date(2026, 10, 26), at("2026-10-26T09:00"))
    store.mark_job_run("summary", date(2026, 10, 27), at("2026-10-27T10:00"))
    # 10/28(수), 10/29(목) 요약은 봇이 꺼져 있어 못 함

    jobs.summary(at("2026-10-30T10:00"))

    texts = [p["text"] for p in admin_posts(slack)]
    missed = [t for t in texts if "놓쳤어요" in t]
    assert len(missed) == 1  # 여러 날을 한 메시지로 묶는다
    assert "10/27(화)" in missed[0] and "10/28(수)" in missed[0]  # 수요일 요약 = 화요일 보고
    assert any("요약" in t and "놓쳤어요" not in t for t in texts)  # 오늘 요약은 정상


def test_설치하기_전_날짜의_요약은_놓쳤다고_하지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-30T10:00")
    store.mark_job_run("installed", date(2026, 10, 30), at("2026-10-30T09:00"))

    jobs.summary(at("2026-10-30T10:00"))

    assert not any("놓쳤어요" in p["text"] for p in admin_posts(slack))


def test_요약한_근무일_다음_날을_휴무일로_바꾸면_그날_보고는_다음_요약에_들어간다(setup):
    jobs, _, slack, store, _, _, summarizer = setup("2026-10-27T10:00")
    store.save_targets(date(2026, 10, 26), {"U1"})
    jobs.summary(at("2026-10-27T10:00"))  # 월요일 요약
    store.add_report("U2", date(2026, 10, 27), "화요일 근무", at("2026-10-27T18:00"))
    store.set_override(date(2026, 10, 27), "off")  # 수요일 아침 화요일을 휴무일로

    jobs.summary(at("2026-10-28T10:00"))

    data = summarizer.calls[-1]
    assert data.workday is None and data.days == [date(2026, 10, 27)]
    assert len(admin_posts(slack)) == 2


def test_그_달_리스트는_하루에_한_번만_시도한다(setup):
    jobs, _, _, _, lists, *_ = setup("2026-11-02T09:00")

    def boom(month):
        lists.ensured.append(month)
        raise RuntimeError("restricted_action")

    lists.ensure_list = boom
    jobs.monthly_list(at("2026-11-02T09:00"))
    jobs.monthly_list(at("2026-11-02T09:01"))

    assert lists.ensured == ["2026-11"]


def test_시작하면_설치일을_기록한다(setup):
    jobs, _, _, store, *_ = setup("2026-10-29T08:00")

    jobs.announce_start()

    assert store.has_job_run("installed", THU)


# 2차 리뷰 반영


def installed(store, day):
    store.mark_job_run("installed", day, at(f"{day.isoformat()}T08:00"))


def test_설치일_이전_날짜는_재알림하지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-26T09:00")
    installed(store, date(2026, 10, 26))

    jobs.reminder(at("2026-10-26T09:00"))  # 재알림 대상은 설치 전인 금요일

    assert dms(slack) == {}
    assert store.get_targets(date(2026, 10, 23)) is None


def test_설치일_이전_날짜는_요약하지_않는다(setup):
    jobs, _, slack, store, _, _, summarizer = setup("2026-10-26T10:00")
    installed(store, date(2026, 10, 26))

    jobs.summary(at("2026-10-26T10:00"))

    assert admin_posts(slack) == [] and summarizer.calls == []
    assert store.has_job_run("summary", date(2026, 10, 26))


def test_놓친_요약_알림이_실패해도_오늘_요약은_올린다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-30T10:00")
    installed(store, date(2026, 10, 26))
    store.save_targets(THU, {"U1"})
    real = slack.chat_postMessage

    def flaky(**kw):
        if "놓쳤어요" in kw.get("text", ""):
            raise RuntimeError("boom")
        return real(**kw)

    slack.chat_postMessage = flaky
    jobs.summary(at("2026-10-30T10:00"))

    assert any("놓쳤어요" not in p["text"] for p in admin_posts(slack))


def test_대상자가_한_명도_없으면_목록을_저장하지_않고_다음에_다시_시도한다(setup):
    jobs, _, _, store, *_ = setup("2026-10-28T17:30", TEST_TARGET_USERS="UTYPO")

    with pytest.raises(RuntimeError, match="대상자"):
        jobs.request(at("2026-10-28T17:30"))

    assert store.get_targets(WED) is None
    assert not store.has_job_run("request", WED)


def test_사용자_목록을_새로_읽지_못하면_대상자_목록을_저장하지_않는다(setup):
    jobs, bot, slack, store, *_ = setup("2026-10-28T17:30")
    bot.directory.members()  # 예전 목록이 캐시에 있음

    def boom(**kw):
        raise RuntimeError("ratelimited")

    slack.users_list = boom
    with pytest.raises(RuntimeError):
        jobs.request(at("2026-10-28T17:30"))

    assert store.get_targets(WED) is None


def test_영구적인_DM_오류는_다시_보내지_않고_작업을_마친다(setup):
    from slack_sdk.errors import SlackApiError

    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    real = slack.chat_postMessage

    def gone(**kw):
        if kw["channel"] == "U2":
            raise SlackApiError("failed", {"ok": False, "error": "user_not_found"})
        return real(**kw)

    slack.chat_postMessage = gone
    jobs.request(at("2026-10-28T17:30"))

    assert store.has_job_run("request", WED)


def test_DM_미리보기_문구에_날짜가_있다(setup):
    jobs, _, slack, *_ = setup("2026-10-28T17:30")

    jobs.request(at("2026-10-28T17:30"))

    assert "10/28(수)" in dms(slack)["U1"]["text"]


def test_요약_게시를_다시_시도할_때_보고가_바뀌었으면_LLM을_다시_부른다(setup):
    jobs, _, slack, store, _, _, summarizer = setup("2026-10-29T10:00")
    store.save_targets(WED, {"U1"})
    real = slack.chat_postMessage

    def boom(**kw):
        raise RuntimeError("not_in_channel")

    slack.chat_postMessage = boom
    with pytest.raises(RuntimeError):
        jobs.summary(at("2026-10-29T10:00"))
    store.add_report("U1", WED, "늦게 냄", at("2026-10-29T10:00"))
    slack.chat_postMessage = real
    jobs.summary(at("2026-10-29T10:01"))

    assert len(summarizer.calls) == 2


def test_요약_기간이_두_달에_걸치면_두_달_리스트_링크를_모두_넣는다(setup):
    jobs, _, slack, store, *_ = setup("2026-11-02T10:00")
    store.save_targets(FRI, {"U1"})

    jobs.summary(at("2026-11-02T10:00"))

    body = json.dumps(admin_posts(slack)[0]["blocks"], ensure_ascii=False)
    assert "https://list/2026-10" in body and "https://list/2026-11" in body


def test_대상자도_보고도_없는_요약은_올리지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T10:00")
    store.save_targets(WED, set())  # 대상자가 없던 날(예: 전원 제외)

    jobs.summary(at("2026-10-29T10:00"))

    assert admin_posts(slack) == []
    assert store.has_job_run("summary", THU)


def test_하루_한_번_리스트_정합성을_점검하고_실패가_있으면_관리자에게_알린다(setup):
    jobs, _, slack, _, lists, *_ = setup("2026-11-02T09:00")
    lists.reconcile_result = {"upserted": 1, "removed": 0, "failed": 2}

    jobs.monthly_list(at("2026-11-02T09:00"))
    jobs.monthly_list(at("2026-11-02T09:01"))

    assert len(lists.reconciled) == 1
    assert any("2건" in p["text"] for p in admin_posts(slack))


def test_보고_요청을_보내지_못한_날의_요약에는_그_사실을_알린다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-29T10:00")
    store.save_targets(WED, {"U1"})

    jobs.summary(at("2026-10-29T10:00"))

    assert "보고 요청 DM을 다 보내지 못했어요" in json.dumps(
        admin_posts(slack)[0]["blocks"], ensure_ascii=False
    )


def test_보고_요청을_보낸_날의_요약에는_알리지_않는다(setup):
    jobs, _, slack, store, *_ = setup("2026-10-28T17:30")
    jobs.request(at("2026-10-28T17:30"))

    jobs.summary(at("2026-10-29T10:00"))

    assert "보고 요청 DM" not in json.dumps(admin_posts(slack)[0]["blocks"], ensure_ascii=False)


def test_주말에_설치했으면_설치_뒤_쉬는_날_보고는_요약에_넣는다(setup):
    sat, sun = date(2026, 10, 31), date(2026, 11, 1)
    jobs, _, slack, store, *_ = setup("2026-11-02T10:00")
    installed(store, sat)
    store.add_report("U1", sun, "장애 대응", at("2026-11-01T12:00"))

    jobs.summary(at("2026-11-02T10:00"))

    body = json.dumps(admin_posts(slack)[0]["blocks"], ensure_ascii=False)
    assert "쉬는 날 보고: u1(일)" in body
    assert "제출" not in body.replace("쉬는 날 보고", "")
    assert store.has_job_run("summary_day", FRI)  # 설치 전 금요일은 다룬 것으로 친다
