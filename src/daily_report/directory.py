"""Slack 사용자 목록과 관리자(관리자 채널 멤버) 캐시.

- 사용자 목록: 일정 시간 캐시한다. 갱신이 실패하면 이전 목록을 계속 쓴다.
- 관리자 목록: 일정 시간 캐시하고 채널 입장·퇴장 이벤트로 바로 고친다.
  관리 메뉴를 실행할 때는 새로 확인하고, 확인이 실패하면 관리자가 아닌 것으로 본다.
"""

import logging
import threading
from datetime import datetime
from typing import Any

from daily_report.clock import Clock
from daily_report.members import Member

log = logging.getLogger(__name__)


class Directory:
    def __init__(self, slack: Any, clock: Clock, admin_channel_id: str, ttl_seconds: int = 600):
        self._slack = slack
        self._clock = clock
        self._admin_channel = admin_channel_id
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._members: dict[str, Member] | None = None
        self._members_at: datetime | None = None
        self._admins: set[str] | None = None
        self._admins_at: datetime | None = None

    def members(self, fresh: bool = False) -> list[Member]:
        with self._lock:
            if fresh or self._expired(self._members_at):
                try:
                    users = self._paged(self._slack.users_list, "members")
                    self._members = {m.id: m for m in map(Member.from_slack, users)}
                    self._members_at = self._clock.now()
                except Exception:
                    # 새로 읽으라고 한 경우(대상자 목록 저장)는 낡은 목록으로 대신하지 않는다
                    if fresh or self._members is None:
                        raise
                    log.exception("사용자 목록 갱신 실패, 이전 목록을 씀")
            return list(self._members.values())

    def knows(self, user_id: str) -> bool:
        """사용자 목록에 있는지. 없으면(방금 입사 등) 한 번 새로 읽어 확인한다."""
        if any(m.id == user_id for m in self.members()):
            return True
        try:
            return any(m.id == user_id for m in self.members(fresh=True))
        except Exception:
            log.exception("사용자 목록 갱신 실패")
            return False

    def by_id(self) -> dict[str, Member]:
        return {m.id: m for m in self.members()}

    def is_admin(self, user_id: str, fresh: bool = False) -> bool:
        with self._lock:
            if fresh or self._expired(self._admins_at):
                try:
                    self._admins = set(
                        self._paged(
                            self._slack.conversations_members,
                            "members",
                            channel=self._admin_channel,
                        )
                    )
                    self._admins_at = self._clock.now()
                except Exception:
                    log.exception("관리자 채널 멤버 확인 실패, 관리자가 아닌 것으로 봄")
                    if fresh or self._admins is None:
                        return False
            return user_id in self._admins

    def member_joined(self, channel_id: str, user_id: str) -> None:
        if channel_id == self._admin_channel:
            with self._lock:
                self.is_admin(user_id)  # 아직 읽지 않았으면 먼저 읽는다
                if self._admins is not None:
                    self._admins.add(user_id)

    def member_left(self, channel_id: str, user_id: str) -> None:
        if channel_id == self._admin_channel:
            with self._lock:
                self.is_admin(user_id)
                if self._admins is not None:
                    self._admins.discard(user_id)

    def _expired(self, loaded_at: datetime | None) -> bool:
        return loaded_at is None or (self._clock.now() - loaded_at).total_seconds() > self._ttl

    @staticmethod
    def _paged(method, key: str, **kwargs) -> list:
        out, cursor = [], None
        while True:
            res = method(cursor=cursor, limit=200, **kwargs)
            out += res[key]
            cursor = res.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return out
