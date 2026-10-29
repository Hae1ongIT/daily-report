import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from slack_sdk import WebClient

from daily_report.clock import FixedClock
from daily_report.config import Config
from daily_report.db import Store
from daily_report.directory import Directory
from daily_report.handlers import Bot

KST = ZoneInfo("Asia/Seoul")

ENV = {
    "SLACK_APP_TOKEN": "xapp",
    "SLACK_BOT_TOKEN": "xoxb",
    "ADMIN_CHANNEL_ID": "CADMIN",
    "LLM_BASE_URL": "http://llm/v1",
    "LLM_MODEL": "m",
}


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=KST)


def user(uid, display="", **kw):
    return {"id": uid, "profile": {"display_name": display or uid.lower(), "real_name": uid}, **kw}


def _check_signature(name, kw):
    """실제 WebClient 메서드가 받는 인자인지 확인한다(인자 이름 오타를 가짜가 숨기지 않게)."""
    inspect.signature(getattr(WebClient, name)).bind(None, **kw)


class FakeSlack:
    """slack_sdk WebClient 중 봇이 쓰는 메서드만 흉내 낸다."""

    def __init__(self, users, admins):
        self.users = users
        self.admins = admins
        self.calls = []
        self.fail = {}  # 메서드 이름 -> Slack 오류 코드

    def _rec(self, name, kw):
        _check_signature(name, kw)
        self.calls.append((name, kw))
        if name in self.fail:
            from slack_sdk.errors import SlackApiError

            raise SlackApiError("failed", {"ok": False, "error": self.fail[name]})
        return {"ok": True, "ts": "1.0"}

    def views_open(self, **kw):
        return self._rec("views_open", kw)

    def views_publish(self, **kw):
        return self._rec("views_publish", kw)

    def chat_postMessage(self, **kw):  # noqa: N802 (slack_sdk 이름)
        return self._rec("chat_postMessage", kw)

    def users_list(self, **kw):
        _check_signature("users_list", kw)
        self.calls.append(("users_list", kw))
        return {"members": self.users, "response_metadata": {"next_cursor": ""}}

    def conversations_members(self, **kw):
        _check_signature("conversations_members", kw)
        self.calls.append(("conversations_members", kw))
        return {"members": self.admins, "response_metadata": {"next_cursor": ""}}

    def of(self, name):
        return [kw for n, kw in self.calls if n == name]

    def last(self, name):
        return self.of(name)[-1]


class FakeLists:
    def __init__(self):
        self.upserted = []
        self.removed = []
        self.synced = []
        self.ensured = []
        self.reconciled = []
        self.reconcile_result = {"upserted": 0, "removed": 0, "failed": 0}
        self.fail = False
        self.fail_permalink = False

    def upsert(self, report_id):
        if self.fail:
            from daily_report.slack_lists import ListError

            raise ListError("down")
        self.upserted.append(report_id)

    def remove(self, report_id):
        self.removed.append(report_id)

    def sync(self, start, end):
        self.synced.append((start, end))
        return {"upserted": 1, "deleted": 0}

    def permalink(self, day):
        if self.fail_permalink:
            from daily_report.slack_lists import ListError

            raise ListError("down")
        return f"https://list/{day:%Y-%m}"

    def ensure_list(self, month):
        self.ensured.append(month)
        return "F1"

    def reconcile(self, since):
        self.reconciled.append(since)
        return self.reconcile_result


@pytest.fixture
def env():
    return dict(ENV)


@pytest.fixture
def make_bot(env):
    def make(now="2026-10-28T18:00", users=None, admins=("UADMIN",), **cfg_env):
        users = users or [user("U1"), user("U2"), user("UADMIN")]
        slack = FakeSlack(users, list(admins))
        clock = FixedClock(at(now))
        cfg = Config.from_env({**env, **cfg_env})
        store = Store(":memory:")
        lists = FakeLists()
        directory = Directory(slack, clock, cfg.admin_channel_id)
        bot = Bot(cfg, store, slack, lists, clock, directory)
        return bot, slack, store, lists, clock

    return make
