import pytest
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from daily_report.app import StartupError, build, llm_client, verify_admin_channel
from daily_report.config import Config
from tests.conftest import ENV


@pytest.fixture
def cfg(tmp_path):
    return Config.from_env({**ENV, "DB_PATH": str(tmp_path / "r.db")})


def test_앱을_조립하면_봇과_예약_작업이_같은_설정을_쓴다(cfg):
    _, bot, jobs, _ = build(cfg, token_verification_enabled=False)

    assert bot.cfg is cfg and jobs.bot is bot  # 리스너 등록은 test_contracts에서 키로 확인


def test_예약_작업은_작업마다_따로_매분_돌고_켜지자마자_한_번_돈다(cfg):
    _, _, jobs, scheduler = build(cfg, token_verification_enabled=False)

    sched = scheduler.get_jobs()
    assert len(sched) == len(jobs.all_jobs())
    assert all(j.trigger.interval.total_seconds() == 60 for j in sched)
    assert all(j.coalesce and j.misfire_grace_time >= 30 for j in sched)


def test_Slack_호출은_레이트리밋이면_다시_시도한다(cfg):
    app, *_ = build(cfg, token_verification_enabled=False)

    assert any(isinstance(h, RateLimitErrorRetryHandler) for h in app.client.retry_handlers)


def test_LLM_클라이언트는_타임아웃을_두고_SDK_자체_재시도는_끈다(cfg):
    client = llm_client(cfg)

    assert client.max_retries == 0
    assert client.timeout == cfg.llm_timeout_seconds


class FakeClient:
    def __init__(self, channel=None, error=None):
        self.channel = channel
        self.error = error

    def conversations_info(self, channel):
        if self.error:
            raise self.error
        return {"channel": self.channel}


def test_관리자_채널이_비공개이고_봇이_멤버면_통과한다():
    verify_admin_channel(FakeClient({"is_private": True, "is_member": True}), "G1")


@pytest.mark.parametrize(
    "channel,reason",
    [
        ({"is_private": False, "is_member": True}, "비공개"),
        ({"is_private": True, "is_member": False}, "초대"),
    ],
)
def test_관리자_채널이_공개거나_봇이_없으면_시작하지_않는다(channel, reason):
    with pytest.raises(StartupError, match=reason):
        verify_admin_channel(FakeClient(channel), "C1")


def test_관리자_채널을_확인할_수_없으면_시작하지_않는다():
    with pytest.raises(StartupError, match="확인할 수 없"):
        verify_admin_channel(FakeClient(error=RuntimeError("channel_not_found")), "C1")


# 2차 리뷰 반영


def test_같은_DB로_두_번째_봇을_띄우면_시작하지_않는다(tmp_path):
    from daily_report.app import single_instance

    first = single_instance(tmp_path / "daily_report.db")
    try:
        with pytest.raises(StartupError, match="이미 실행 중"):
            single_instance(tmp_path / "daily_report.db")
    finally:
        first.close()

    single_instance(tmp_path / "daily_report.db").close()  # 첫 봇이 끝나면 다시 띄울 수 있다


def test_예약_작업에는_로그에서_구분되는_이름이_붙는다(cfg):
    _, _, _, scheduler = build(cfg, token_verification_enabled=False)

    assert sorted(j.name for j in scheduler.get_jobs()) == [
        "monthly_list",
        "reminder",
        "request",
        "summary",
    ]


def test_사용자_목록에_없는_시험_대상자를_알려준다():
    from daily_report.app import unknown_test_users
    from daily_report.members import Member

    members = [Member("U1", "a", "A")]

    assert unknown_test_users(("U1", "UTYPO"), members) == ["UTYPO"]
