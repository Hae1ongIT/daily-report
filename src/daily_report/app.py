"""진입점: Slack Bolt(Socket Mode)와 예약 작업을 연결한다.

실행: uv run python -m daily_report.app
"""

import logging
import os
import signal
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from openai import OpenAI
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from daily_report import views
from daily_report.clock import SystemClock
from daily_report.config import Config, ConfigError
from daily_report.db import Store
from daily_report.directory import Directory
from daily_report.handlers import Bot
from daily_report.jobs import Jobs, run_safely
from daily_report.llm import Summarizer, openai_complete
from daily_report.members import Member
from daily_report.slack_lists import ListSync

log = logging.getLogger(__name__)

# .env와 상대 DB 경로의 기준 폴더. 소스 폴더 밖에 설치해 실행하면 DAILY_REPORT_HOME으로 지정한다.
PROJECT_DIR = Path(os.environ.get("DAILY_REPORT_HOME") or Path(__file__).resolve().parents[2])

# 설정 오류처럼 다시 시작해도 소용없는 종료 코드(systemd RestartPreventExitStatus=78)
EXIT_CONFIG = 78


class StartupError(RuntimeError):
    pass


def llm_client(cfg: Config) -> OpenAI:
    # 재시도는 Summarizer가 SPEC대로(간격·횟수) 하므로 SDK 자체 재시도는 끈다
    return OpenAI(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        timeout=cfg.llm_timeout_seconds,
        max_retries=0,
    )


def build(cfg: Config, token_verification_enabled: bool = True):
    client = WebClient(
        token=cfg.slack_bot_token,
        retry_handlers=[RateLimitErrorRetryHandler(max_retry_count=3)],
    )
    app = App(
        client=client,
        token_verification_enabled=token_verification_enabled,
        # ack 뒤 처리(리스트 기록 등)가 워커를 잡아도 새 버튼 응답이 밀리지 않게 넉넉히 둔다
        listener_executor=ThreadPoolExecutor(max_workers=20),
    )
    clock = SystemClock(cfg.timezone)
    store = Store(cfg.db_path)
    directory = Directory(app.client, clock, cfg.admin_channel_id)
    lists = ListSync(
        lambda method, payload: app.client.api_call(method, json=payload).data,
        store,
        cfg.admin_channel_id,
    )
    summarizer = Summarizer(
        openai_complete(llm_client(cfg), cfg.llm_model),
        cfg.llm_retry_count,
        cfg.llm_retry_interval_seconds,
    )
    bot = Bot(cfg, store, app.client, lists, clock, directory)
    jobs = Jobs(bot, summarizer)
    _register(app, bot)

    # 작업마다 시각·시한·실행 기록을 스스로 확인하므로 매분 불러도 된다. 작업을 따로 두어
    # 요약의 LLM 대기가 요청·재알림을 막지 않게 하고, 켜지자마자 한 번씩 돌려 늦은 실행을 한다.
    scheduler = BackgroundScheduler(timezone=cfg.timezone)
    for job in jobs.all_jobs():
        scheduler.add_job(
            lambda job=job: run_safely(job, clock.now()),
            "interval",
            seconds=60,
            next_run_time=clock.now(),
            coalesce=True,
            misfire_grace_time=30,
            id=job.__name__,
            name=job.__name__,
        )
    return app, bot, jobs, scheduler


def single_instance(db_path: Path):
    """같은 DB로 봇이 둘 뜨면 DM·요약이 두 번 나가므로 막는다. 돌려준 파일을 닫으면 풀린다."""
    lock_path = Path(db_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")  # noqa: SIM115 (프로세스가 끝날 때까지 열어 둔다)
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        handle.close()
        raise StartupError(f"이미 실행 중인 봇이 있어요({lock_path}).") from e
    return handle


def unknown_test_users(test_users: tuple[str, ...], members: list[Member]) -> list[str]:
    known = {m.id for m in members}
    return [u for u in test_users if u not in known]


def verify_admin_channel(client, channel_id: str) -> None:
    """요약과 원본 리스트가 나가는 채널이라, 공개 채널이거나 봇이 없으면 시작하지 않는다."""
    try:
        channel = client.conversations_info(channel=channel_id)["channel"]
    except Exception as e:
        raise StartupError(f"관리자 채널 {channel_id}를 확인할 수 없어요: {e}") from e
    if not channel.get("is_private"):
        raise StartupError(f"관리자 채널 {channel_id}는 비공개 채널이어야 해요.")
    if not channel.get("is_member"):
        raise StartupError(f"관리자 채널 {channel_id}에 봇을 초대해 주세요.")


def _register(app: App, bot: Bot) -> None:
    def uid(body):
        return body["user"]["id"]

    def bolt_ack(ack):
        def respond(errors=None):
            if errors:
                ack(response_action="errors", errors=errors)
            else:
                ack()

        return respond

    @app.event("app_home_opened")
    def home(event):
        if event.get("tab") == "home":
            bot.home_opened(event["user"])

    @app.event("member_joined_channel")
    def joined(event):
        bot.member_joined(event["channel"], event["user"])

    @app.event("member_left_channel")
    def left(event):
        bot.member_left(event["channel"], event["user"])

    for action_id in (views.OPEN_REPORT, views.WRITE_REPORT):

        @app.action(action_id)
        def open_report(ack, body, action):
            ack()
            bot.open_report(uid(body), body["trigger_id"], action.get("value", ""))

    @app.action(views.EDIT_REPORT)
    def edit_report(ack, body, action):
        ack()
        bot.edit_report(uid(body), body["trigger_id"], action["value"])

    @app.action(views.DELETE_REPORT)
    def remove_report(ack, body, action):
        ack()
        bot.delete_report(uid(body), action["value"])

    @app.action(views.OPEN_LEAVE)
    def open_leave(ack, body):
        ack()
        bot.open_leave(uid(body), body["trigger_id"], register=True, for_other=False)

    @app.action(views.OPEN_LEAVE_CANCEL)
    def open_leave_cancel(ack, body):
        ack()
        bot.open_leave(uid(body), body["trigger_id"], register=False, for_other=False)

    for action_id in (
        views.ADMIN_EXCLUSIONS,
        views.ADMIN_CALENDAR,
        views.ADMIN_SYNC,
        views.ADMIN_LEAVE,
        views.ADMIN_LEAVE_CANCEL,
        views.ADMIN_LEAVE_STATUS,
    ):

        @app.action(action_id)
        def open_admin(ack, body, action):
            ack()
            bot.open_admin(uid(body), body["trigger_id"], action["action_id"])

    @app.view(views.REPORT_SUBMIT)
    def report_submit(ack, body, view):
        bot.submit_report(uid(body), view, ack=bolt_ack(ack))

    @app.view(views.LEAVE_SUBMIT)
    def leave_submit(ack, body, view):
        bot.submit_leave(uid(body), view, ack=bolt_ack(ack))

    @app.view(views.EXCLUSIONS_SUBMIT)
    def exclusions_submit(ack, body, view):
        bot.submit_exclusions(uid(body), view, ack=bolt_ack(ack))

    @app.view(views.CALENDAR_SUBMIT)
    def calendar_submit(ack, body, view):
        bot.submit_calendar(uid(body), view, ack=bolt_ack(ack))

    @app.view(views.SYNC_SUBMIT)
    def sync_submit(ack, body, view):
        errors = bot.validate_sync(uid(body), view)
        bolt_ack(ack)(errors)
        if not errors:
            bot.run_sync(uid(body), view)


def main() -> None:
    os.umask(0o077)  # DB(업무 보고)와 백업 파일을 다른 계정이 읽지 못하게 한다
    load_dotenv(PROJECT_DIR / ".env")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # DEBUG로 올리면 Slack 요청 본문(보고 내용)이 로그에 찍힐 수 있다
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # 매분 실행 로그가 쌓이지 않게

    try:
        cfg = Config.from_env(os.environ, base_dir=PROJECT_DIR)
    except ConfigError as e:
        log.error("설정 오류: %s", e)
        sys.exit(EXIT_CONFIG)
    try:
        lock = single_instance(cfg.db_path)
        app, bot, jobs, scheduler = build(cfg)
        verify_admin_channel(app.client, cfg.admin_channel_id)
    except StartupError as e:
        log.error("시작할 수 없어요: %s", e)
        sys.exit(1)

    unknown = unknown_test_users(cfg.test_target_users, bot.directory.members())
    if unknown:
        log.warning("사용자 목록에 없는 시험 대상자 ID: %s", ", ".join(unknown))
    log.info(
        "시작: pid %d, 호스트 %s, 요청 %s(~%s), 재알림 %s(~%s), 요약 %s(~%s), "
        "수정 기간 근무일 %d일, 시간대 %s, DB %s, LLM %s, 시험 모드 %s",
        os.getpid(),
        socket.gethostname(),
        cfg.request_time,
        cfg.request_deadline,
        cfg.reminder_time,
        cfg.reminder_deadline,
        cfg.summary_time,
        cfg.summary_deadline,
        cfg.edit_window_workdays,
        cfg.timezone,
        cfg.db_path.resolve(),
        urlparse(cfg.llm_base_url).netloc,
        f"{len(cfg.test_target_users)}명" if cfg.test_mode else "꺼짐",
    )

    # systemd의 정지 신호(SIGTERM)도 Ctrl+C처럼 정리 단계를 거치게 한다
    def stop(signum, frame):
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    jobs.announce_start()
    scheduler.start()
    handler = SocketModeHandler(app, cfg.slack_app_token)
    try:
        handler.start()
    except KeyboardInterrupt:
        pass
    finally:
        # LLM 재시도 대기는 바로 멈추고(게시하지 않고 다음 실행에서 다시 한다), 진행 중인
        # 작업이 끝나기를 기다린다. LLM 요청 하나가 진행 중이면 최대 LLM_TIMEOUT_SECONDS.
        jobs.summarizer.cancel()
        scheduler.shutdown(wait=True)
        handler.close()
        lock.close()
        log.info("daily-report 종료")


if __name__ == "__main__":
    main()
