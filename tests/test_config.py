from datetime import time

import pytest

from daily_report.config import Config, ConfigError

REQUIRED = {
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "ADMIN_CHANNEL_ID": "C0ADMIN",
    "LLM_BASE_URL": "http://llm.local/v1",
    "LLM_MODEL": "test-model",
}


def test_필수값만_있으면_SPEC_기본값으로_채운다():
    cfg = Config.from_env(REQUIRED)

    assert cfg.request_time == time(17, 30)
    assert cfg.reminder_time == time(9, 0)
    assert cfg.summary_time == time(10, 0)
    assert cfg.request_deadline == time(21, 0)
    assert cfg.summary_deadline == time(18, 0)
    assert cfg.edit_window_workdays == 5
    assert cfg.llm_retry_count == 3
    assert cfg.llm_retry_interval_seconds == 60
    assert cfg.test_target_users == ()
    assert cfg.timezone == "Asia/Seoul"


def test_재알림_시한의_기본값은_요약_시각이다():
    cfg = Config.from_env({**REQUIRED, "SUMMARY_TIME": "10:30"})

    assert cfg.reminder_deadline == time(10, 30)


def test_시각과_숫자_설정을_바꿀_수_있다():
    cfg = Config.from_env(
        {
            **REQUIRED,
            "REQUEST_TIME": "17:00",
            "REMINDER_TIME": "08:50",
            "EDIT_WINDOW_WORKDAYS": "3",
            "LLM_RETRY_COUNT": "5",
        }
    )

    assert cfg.request_time == time(17, 0)
    assert cfg.reminder_time == time(8, 50)
    assert cfg.edit_window_workdays == 3
    assert cfg.llm_retry_count == 5


def test_시험_대상자는_쉼표로_구분한다():
    cfg = Config.from_env({**REQUIRED, "TEST_TARGET_USERS": " U1, U2 ,,U3 "})

    assert cfg.test_target_users == ("U1", "U2", "U3")
    assert cfg.test_mode is True


def test_시험_대상자가_비어_있으면_시험_모드가_아니다():
    cfg = Config.from_env({**REQUIRED, "TEST_TARGET_USERS": "  "})

    assert cfg.test_mode is False


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_필수값이_없으면_오류(missing):
    env = {k: v for k, v in REQUIRED.items() if k != missing}

    with pytest.raises(ConfigError, match=missing):
        Config.from_env(env)


def test_시각_형식이_틀리면_오류():
    with pytest.raises(ConfigError, match="REQUEST_TIME"):
        Config.from_env({**REQUIRED, "REQUEST_TIME": "5시반"})


def test_토큰과_키는_출력되지_않는다():
    cfg = Config.from_env({**REQUIRED, "LLM_API_KEY": "sk-secret"})

    text = repr(cfg)
    assert "xoxb-test" not in text and "xapp-test" not in text and "sk-secret" not in text


@pytest.mark.parametrize(
    "key,value",
    [
        ("REQUEST_DEADLINE", "17:00"),  # 요청 시각(17:30)보다 이름
        ("SUMMARY_DEADLINE", "09:00"),  # 요약 시각(10:00)보다 이름
        ("REMINDER_DEADLINE", "08:00"),  # 재알림 시각(09:00)보다 이름
    ],
)
def test_시한이_시각보다_이르면_오류(key, value):
    with pytest.raises(ConfigError, match=key):
        Config.from_env({**REQUIRED, key: value})


def test_재알림_시각이_요약_시각보다_늦으면_오류():
    with pytest.raises(ConfigError, match="REMINDER_TIME"):
        Config.from_env({**REQUIRED, "REMINDER_TIME": "10:30"})


@pytest.mark.parametrize(
    "key,value",
    [("EDIT_WINDOW_WORKDAYS", "0"), ("LLM_RETRY_COUNT", "-1"), ("LLM_TIMEOUT_SECONDS", "0")],
)
def test_숫자_설정_범위를_확인한다(key, value):
    with pytest.raises(ConfigError, match=key):
        Config.from_env({**REQUIRED, key: value})


def test_시간대가_틀리면_오류():
    with pytest.raises(ConfigError, match="TIMEZONE"):
        Config.from_env({**REQUIRED, "TIMEZONE": "Asia/Nowhere"})


def test_관리자_채널_ID_형식을_확인한다():
    with pytest.raises(ConfigError, match="ADMIN_CHANNEL_ID"):
        Config.from_env({**REQUIRED, "ADMIN_CHANNEL_ID": "#관리자"})


def test_상대_DB_경로는_프로젝트_폴더_기준이다(tmp_path):
    cfg = Config.from_env(REQUIRED, base_dir=tmp_path)
    absolute = Config.from_env({**REQUIRED, "DB_PATH": str(tmp_path / "x.db")}, base_dir=tmp_path)

    assert cfg.db_path == tmp_path / "data" / "daily_report.db"
    assert absolute.db_path == tmp_path / "x.db"


def test_LLM_타임아웃_기본값은_120초다():
    assert Config.from_env(REQUIRED).llm_timeout_seconds == 120


def test_수정_가능_기간은_앱_홈_화면_한도_때문에_15근무일까지다():
    assert Config.from_env({**REQUIRED, "EDIT_WINDOW_WORKDAYS": "15"}).edit_window_workdays == 15
    with pytest.raises(ConfigError):
        Config.from_env({**REQUIRED, "EDIT_WINDOW_WORKDAYS": "16"})
