from datetime import timedelta

import pytest
from slack_sdk.errors import SlackApiError

from daily_report.clock import FixedClock
from daily_report.directory import Directory
from tests.conftest import FakeSlack, at, user


@pytest.fixture
def env():
    slack = FakeSlack([user("U1")], ["UADMIN"])
    clock = FixedClock(at("2026-10-28T09:00"))
    return Directory(slack, clock, "CADMIN", ttl_seconds=600), slack, clock


def fail(*a, **k):
    raise SlackApiError("failed", {"ok": False, "error": "boom"})


def test_사용자_목록은_정해진_시간_동안_캐시한다(env):
    d, slack, clock = env

    d.members()
    d.members()
    clock.at += timedelta(seconds=601)
    d.members()

    assert len(slack.of("users_list")) == 2


def test_새로_읽으라고_하면_캐시를_무시한다(env):
    d, slack, _ = env

    d.members()
    d.members(fresh=True)

    assert len(slack.of("users_list")) == 2


def test_사용자_목록_갱신이_실패하면_이전_목록을_쓴다(env):
    d, slack, clock = env
    d.members()
    slack.users_list = fail
    clock.at += timedelta(seconds=601)

    assert [m.id for m in d.members()] == ["U1"]


def test_관리자_목록도_정해진_시간이_지나면_다시_읽는다(env):
    d, slack, clock = env
    assert d.is_admin("UADMIN")

    slack.admins = ["UNEW"]  # 이벤트를 놓친 사이 관리자가 바뀜
    clock.at += timedelta(seconds=601)

    assert d.is_admin("UNEW") and not d.is_admin("UADMIN")


def test_관리_메뉴를_실행할_때는_관리자_목록을_새로_확인한다(env):
    d, slack, _ = env
    assert d.is_admin("UADMIN")
    slack.admins = []

    assert not d.is_admin("UADMIN", fresh=True)


def test_관리자_확인이_실패하면_관리자가_아닌_것으로_본다(env):
    d, slack, _ = env
    slack.conversations_members = fail

    assert d.is_admin("UADMIN") is False
    assert d.is_admin("UADMIN", fresh=True) is False


def test_관리자_채널_입장_퇴장_이벤트를_바로_반영한다(env):
    d, *_ = env

    d.member_joined("CADMIN", "U1")
    assert d.is_admin("U1")
    d.member_left("CADMIN", "U1")
    d.member_joined("COTHER", "U2")

    assert not d.is_admin("U1") and not d.is_admin("U2")


def test_새로_읽으라고_했는데_실패하면_이전_목록을_쓰지_않고_오류를_낸다(env):
    d, slack, _ = env
    d.members()
    slack.users_list = fail

    with pytest.raises(SlackApiError):
        d.members(fresh=True)


def test_사용자_ID가_목록에_있는지_확인하고_없으면_새로_읽는다(env):
    d, slack, _ = env
    d.members()
    slack.users.append(user("UNEW"))

    assert d.knows("UNEW")
    assert len(slack.of("users_list")) == 2
