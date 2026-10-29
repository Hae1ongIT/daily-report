"""Slack 사용자 정보 중 봇이 쓰는 부분."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Member:
    id: str
    display_name: str
    real_name: str
    is_bot: bool = False
    is_guest: bool = False
    deleted: bool = False

    @property
    def name(self) -> str:
        """요약에 쓰는 이름: 표시 이름, 비어 있으면 실명."""
        return self.display_name or self.real_name or self.id

    @classmethod
    def from_slack(cls, user: Mapping[str, Any]) -> "Member":
        profile = user.get("profile", {})
        return cls(
            id=user["id"],
            display_name=(profile.get("display_name") or "").strip(),
            real_name=(profile.get("real_name") or user.get("real_name") or "").strip(),
            # Slackbot(USLACKBOT)은 is_bot이 False로 온다
            is_bot=bool(user.get("is_bot")) or user["id"] == "USLACKBOT",
            is_guest=bool(user.get("is_restricted") or user.get("is_ultra_restricted")),
            deleted=bool(user.get("deleted")),
        )
