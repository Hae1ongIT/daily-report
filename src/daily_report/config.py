"""`.env` 설정 로딩. 기본값은 SPEC.md 결정 사항을 따른다."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REQUIRED_KEYS = (
    "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN",
    "ADMIN_CHANNEL_ID",
    "LLM_BASE_URL",
    "LLM_MODEL",
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    # 비밀값은 repr(로그, 오류 메시지)에 나오지 않게 한다
    slack_app_token: str = field(repr=False)
    slack_bot_token: str = field(repr=False)
    admin_channel_id: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str = field(repr=False)
    request_time: time
    reminder_time: time
    summary_time: time
    request_deadline: time
    reminder_deadline: time
    summary_deadline: time
    edit_window_workdays: int
    llm_retry_count: int
    llm_retry_interval_seconds: int
    llm_timeout_seconds: int
    test_target_users: tuple[str, ...]
    timezone: str
    db_path: Path

    @property
    def test_mode(self) -> bool:
        return bool(self.test_target_users)

    @classmethod
    def from_env(cls, env: Mapping[str, str], base_dir: Path | None = None) -> "Config":
        """base_dir는 상대 DB 경로의 기준(프로젝트 폴더). 없으면 현재 작업 폴더."""
        missing = [k for k in REQUIRED_KEYS if not env.get(k, "").strip()]
        if missing:
            raise ConfigError(f"필수 설정이 없습니다: {', '.join(missing)}")

        admin_channel = env["ADMIN_CHANNEL_ID"].strip()
        if not re.fullmatch(r"[CG][A-Z0-9]+", admin_channel):
            raise ConfigError(
                f"ADMIN_CHANNEL_ID는 채널 ID(C… 또는 G…)여야 합니다: {admin_channel!r}"
            )

        timezone = env.get("TIMEZONE", "").strip() or "Asia/Seoul"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(f"TIMEZONE을 찾을 수 없습니다: {timezone!r}") from e

        summary_time = _time(env, "SUMMARY_TIME", "10:00")
        times = {
            "request_time": _time(env, "REQUEST_TIME", "17:30"),
            "reminder_time": _time(env, "REMINDER_TIME", "09:00"),
            "summary_time": summary_time,
            "request_deadline": _time(env, "REQUEST_DEADLINE", "21:00"),
            "reminder_deadline": _time(env, "REMINDER_DEADLINE", summary_time.strftime("%H:%M")),
            "summary_deadline": _time(env, "SUMMARY_DEADLINE", "18:00"),
        }
        for start, deadline in [
            ("REQUEST_TIME", "REQUEST_DEADLINE"),
            ("REMINDER_TIME", "REMINDER_DEADLINE"),
            ("SUMMARY_TIME", "SUMMARY_DEADLINE"),
        ]:
            if times[deadline.lower()] < times[start.lower()]:
                raise ConfigError(f"{deadline}는 {start}보다 늦어야 합니다.")

        db_path = Path(env.get("DB_PATH", "").strip() or "data/daily_report.db")
        if not db_path.is_absolute() and base_dir is not None:
            db_path = base_dir / db_path

        return cls(
            slack_app_token=env["SLACK_APP_TOKEN"].strip(),
            slack_bot_token=env["SLACK_BOT_TOKEN"].strip(),
            admin_channel_id=admin_channel,
            llm_base_url=env["LLM_BASE_URL"].strip(),
            llm_model=env["LLM_MODEL"].strip(),
            # vLLM은 키를 요구하지 않는 경우가 많다. openai SDK는 빈 키를 거부하므로 자리값을 둔다.
            llm_api_key=env.get("LLM_API_KEY", "").strip() or "EMPTY",
            **times,
            # 앱 홈은 Slack 한도(100블록) 안에 날짜 행을 모두 그려야 한다
            edit_window_workdays=_int(env, "EDIT_WINDOW_WORKDAYS", 5, minimum=1, maximum=15),
            llm_retry_count=_int(env, "LLM_RETRY_COUNT", 3, minimum=0),
            llm_retry_interval_seconds=_int(env, "LLM_RETRY_INTERVAL_SECONDS", 60, minimum=0),
            llm_timeout_seconds=_int(env, "LLM_TIMEOUT_SECONDS", 120, minimum=1),
            test_target_users=tuple(
                u.strip() for u in env.get("TEST_TARGET_USERS", "").split(",") if u.strip()
            ),
            timezone=timezone,
            db_path=db_path,
        )


def _time(env: Mapping[str, str], key: str, default: str) -> time:
    raw = env.get(key, "").strip() or default
    try:
        hour, minute = raw.split(":")
        return time(int(hour), int(minute))
    except ValueError as e:
        raise ConfigError(f"{key} 형식은 HH:MM 이어야 합니다: {raw!r}") from e


def _int(
    env: Mapping[str, str], key: str, default: int, minimum: int, maximum: int | None = None
) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} 는 정수여야 합니다: {raw!r}") from e
    if value < minimum:
        raise ConfigError(f"{key} 는 {minimum} 이상이어야 합니다: {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} 는 {maximum} 이하여야 합니다: {value}")
    return value
