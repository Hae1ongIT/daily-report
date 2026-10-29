"""예약 작업: 보고 요청(기본 17:30), 재알림(기본 09:00), 요약(기본 10:00), 리스트 관리.

각 작업은 "지금 시각"을 받아 스스로 판단한다(근무일인지, 시각 전인지, 시한이 지났는지,
이미 실행했는지). 스케줄러가 작업마다 1분마다 부르고, 켜지자마자 한 번 부르므로
정시 실행과 다시 켜졌을 때의 늦은 실행이 같은 함수를 쓴다.

실행 기록(job_runs)의 날짜 키
- request: 요청한 날, reminder: 재알림 대상 보고 날짜
- summary: 요약을 올리는 날, summary_day: 요약에서 다룬 각 날짜
- monthly_list: 리스트 관리(그 달 리스트 준비, 정합성 점검)를 한 날
- installed: 봇을 켠 날(가장 이른 날 이전은 대상자 목록이 생길 수 없었던 날로 본다)
"""

import logging
from datetime import date, datetime, timedelta

from slack_sdk.errors import SlackApiError

from daily_report import views
from daily_report.db import ANNUAL, HALF
from daily_report.fmt import month_key
from daily_report.handlers import Bot
from daily_report.slack_lists import months_between
from daily_report.summary import LlmSummary, SummaryData, collect_summary
from daily_report.targets import ensure_targets

log = logging.getLogger(__name__)

# 놓친 요약을 거슬러 확인하는 최대 일수
MISSED_LOOKBACK_DAYS = 31
# 리스트 정합성 점검이 다시 맞추는 기간(최근 며칠의 보고)
RECONCILE_DAYS = 45
# 다시 보내도 소용없는 DM 오류(받는 사람이 없거나 비활성)
PERMANENT_DM_ERRORS = {
    "user_not_found",
    "user_disabled",
    "account_inactive",
    "cannot_dm_bot",
    "channel_not_found",
}


class Jobs:
    def __init__(self, bot: Bot, summarizer):
        self.bot = bot
        self.summarizer = summarizer
        # 게시가 실패해도 다음 실행에서 LLM을 다시 부르지 않도록 보관한다.
        # (요약 날짜, 보고 지문)이 같을 때만 다시 쓰고, 보고가 바뀌면 다시 요약한다.
        self._llm_cache: tuple[date, tuple, LlmSummary | None] | None = None

    @property
    def _cfg(self):
        return self.bot.cfg

    def _fresh_targets(self) -> set[str]:
        return self.bot.current_targets(fresh=True)

    def _before_install(self, day: date) -> bool:
        installed = self.bot.store.first_job_day("installed")
        return installed is not None and day < installed

    # 보고 요청

    def request(self, now: datetime) -> None:
        store, cal, today = self.bot.store, self.bot.calendar(), now.date()
        if not cal.is_workday(today) or store.has_job_run("request", today):
            return
        if not self._cfg.request_time <= now.time() <= self._cfg.request_deadline:
            return
        targets = ensure_targets(store, today, self._fresh_targets)
        recipients = [
            uid
            for uid in sorted(targets)
            if store.get_leave(uid, today) != ANNUAL and not store.get_report(uid, today)
        ]
        self._send_all("request", today, recipients, now)

    # 재알림

    def reminder(self, now: datetime) -> None:
        store, cal, today = self.bot.store, self.bot.calendar(), now.date()
        if not cal.is_workday(today):
            return
        if not self._cfg.reminder_time <= now.time() <= self._cfg.reminder_deadline:
            return
        day = cal.prev_workday(today)
        if store.has_job_run("reminder", day):
            return
        if self._before_install(day):  # 봇이 없던 날의 보고는 요청한 적도 없다
            store.mark_job_run("reminder", day, now)
            return
        targets = ensure_targets(store, day, self._fresh_targets)
        recipients = [
            uid
            for uid in sorted(targets)
            if store.get_leave(uid, today) != ANNUAL  # 재알림 날 연차면 보내지 않는다
            and store.get_leave(uid, day) not in (ANNUAL, HALF)  # 미제출이 아니다
            and not store.get_report(uid, day)
        ]
        self._send_all("reminder", day, recipients, now)

    def _send_all(self, job: str, day: date, recipients, now: datetime) -> None:
        """사람마다 보낸 기록을 남긴다. 실패한 사람이 있으면 완료로 표시하지 않아서
        다음 실행(시한 안)에 실패한 사람에게만 다시 보낸다. 받는 사람이 없거나 비활성인
        영구 오류는 다시 보내도 소용없으므로 보낸 것으로 친다."""
        store = self.bot.store
        if job == "request":
            text, blocks = views.request_text(day), views.request_dm(day)
        else:
            summary_time = self._cfg.summary_time
            text = views.reminder_text(day, summary_time)
            blocks = views.reminder_dm(day, summary_time)
        sent = failed = skipped = 0
        for uid in recipients:
            if store.dm_sent(job, day, uid):
                continue
            try:
                self.bot.slack.chat_postMessage(channel=uid, text=text, blocks=blocks)
            except SlackApiError as e:
                if e.response.get("error") in PERMANENT_DM_ERRORS:
                    log.warning("%s DM 건너뜀(%s): %s", job, e.response.get("error"), uid)
                    store.record_dm(job, day, uid, now)
                    skipped += 1
                else:
                    log.exception("%s DM 실패: %s", job, uid)
                    failed += 1
                continue
            except Exception:
                log.exception("%s DM 실패: %s", job, uid)
                failed += 1
                continue
            store.record_dm(job, day, uid, now)
            sent += 1
        if failed == 0:
            store.mark_job_run(job, day, now)
        log.info(
            "%s %s: 보냄 %d, 실패 %d, 건너뜀 %d, 대상 %d",
            job,
            day,
            sent,
            failed,
            skipped,
            len(recipients),
        )

    # 요약

    def summary(self, now: datetime) -> None:
        store, cal, today = self.bot.store, self.bot.calendar(), now.date()
        if not cal.is_workday(today) or now.time() < self._cfg.summary_time:
            return
        try:
            self._notify_missed(cal, today, now)
        except Exception:  # 놓친 요약 알림이 실패해도 오늘 요약은 올린다
            log.exception("놓친 요약 알림 실패")
        if store.has_job_run("summary", today):
            return
        span = cal.summary_range(today)
        if self._before_install(span[-1]):  # 봇이 없던 날은 요약하지 않는다
            store.mark_job_run("summary", today, now)
            return
        # 설치 전 날짜는 다룬 것으로 친다(예: 토요일에 설치하면 금요일은 빼고 주말 보고만)
        for d in span:
            if self._before_install(d):
                store.mark_job_run("summary_day", d, now)
        if now.time() > self._cfg.summary_deadline:
            self._post_missed([(today, span)], now)
            return

        already = {d for d in span if store.has_job_run("summary_day", d)}
        if set(span) <= already:
            store.mark_job_run("summary", today, now)
            return
        if span[0] not in already:
            ensure_targets(store, span[0], self._fresh_targets)
        data = collect_summary(store, cal, today, self.bot.directory.by_id(), already)
        if data.total == 0 and not data.people and not data.annual and not data.half_missing:
            log.info("요약할 대상자도 보고도 없음: %s", today)
            self._mark_summary(today, data.days, now)
            return

        llm = self._summarize(today, data)
        blocks, text = views.summary_message(data, llm, self._links(data.days))
        self.bot.slack.chat_postMessage(
            channel=self._cfg.admin_channel_id, text=text, blocks=blocks
        )
        self._mark_summary(today, data.days, now)
        self._llm_cache = None
        log.info(
            "요약 게시 %s: 제출 %d/%d, LLM %s",
            today,
            data.submitted,
            data.total,
            "실패" if llm is None else "성공",
        )

    def _summarize(self, today: date, data: SummaryData) -> LlmSummary | None:
        fingerprint = tuple((p.user_id, tuple(p.entries)) for p in data.people)
        cached = self._llm_cache
        if cached and cached[0] == today and cached[1] == fingerprint:
            return cached[2]
        llm = self.summarizer.summarize(data)
        self._llm_cache = (today, fingerprint, llm)
        return llm

    def _links(self, days: list[date]) -> list[tuple[str, str]]:
        links = []
        for month in months_between(days[0], days[-1]):
            first = date.fromisoformat(f"{month}-01")
            try:
                url = self.bot.lists.permalink(max(first, days[0]))
            except Exception:
                log.exception("리스트 링크를 가져오지 못함: %s", month)
                continue
            if url:
                links.append((f"{first.month}월", url))
        return links

    def _notify_missed(self, cal, today: date, now: datetime) -> None:
        """봇이 꺼져 있어 지나간 근무일의 요약을 놓쳤으면 한 메시지로 알린다."""
        store = self.bot.store
        installed = store.first_job_day("installed")
        if installed is None:
            return
        done = store.job_days("summary", today - timedelta(days=MISSED_LOOKBACK_DAYS * 2))
        missed = []
        day = today
        for _ in range(MISSED_LOOKBACK_DAYS):
            day = cal.prev_workday(day)
            span = cal.summary_range(day)
            if span[0] < installed:
                break
            if day not in done:
                missed.append((day, span))
        if missed:
            self._post_missed(sorted(missed), now)

    def _post_missed(self, summaries: list[tuple[date, list[date]]], now: datetime) -> None:
        """summaries: (요약 날짜, 그 요약이 다룰 날짜들). 아직 다루지 않은 날짜를 알린다."""
        store = self.bot.store
        uncovered = [
            (summary_day, [d for d in span if not store.has_job_run("summary_day", d)])
            for summary_day, span in summaries
        ]
        days = [days[0] for _, days in uncovered if days]
        if days:
            log.warning("요약을 놓침: %s", days)
            self.bot.slack.chat_postMessage(
                channel=self._cfg.admin_channel_id, text=views.missed_summary_text(days)
            )
        for summary_day, covered in uncovered:
            self._mark_summary(summary_day, covered, now)

    def _mark_summary(self, summary_day: date, days, now: datetime) -> None:
        store = self.bot.store
        for d in days:
            store.mark_job_run("summary_day", d, now)
        store.mark_job_run("summary", summary_day, now)

    # 리스트 관리

    def monthly_list(self, now: datetime) -> None:
        """하루에 한 번: 그 달 리스트를 준비하고, DB와 어긋난 리스트 행을 맞춘다.
        실패해도 보고를 기록할 때 다시 만들고, 어긋남이 남으면 관리자에게 알린다."""
        store, today = self.bot.store, now.date()
        if store.has_job_run("monthly_list", today):
            return
        store.mark_job_run("monthly_list", today, now)
        try:
            self.bot.lists.ensure_list(month_key(today))
        except Exception:
            log.exception("그 달 리스트 준비 실패")
        try:
            result = self.bot.lists.reconcile(since=today - timedelta(days=RECONCILE_DAYS))
        except Exception:
            log.exception("리스트 정합성 점검 실패")
            return
        log.info("리스트 정합성 점검: %s", result)
        if result["failed"]:
            self.bot.slack.chat_postMessage(
                channel=self._cfg.admin_channel_id,
                text=(
                    f"관리자 리스트에 DB와 맞추지 못한 보고가 {result['failed']}건 있어요. "
                    "앱 홈 > 관리 메뉴 > 리스트 동기화로 최근 기간을 맞춰 주세요."
                ),
            )

    # 시작

    def all_jobs(self):
        return [self.request, self.reminder, self.summary, self.monthly_list]

    def catch_up(self, now: datetime) -> None:
        """모든 작업을 한 번씩 실행한다(테스트·수동 점검용. 운영에서는 스케줄러가
        작업마다 켜지자마자 한 번, 그 뒤 1분마다 부른다)."""
        for job in self.all_jobs():
            run_safely(job, now)

    def announce_start(self) -> None:
        now = self.bot.clock.now()
        self.bot.store.mark_job_run("installed", now.date(), now)
        if self._cfg.test_mode:
            try:
                self.bot.slack.chat_postMessage(
                    channel=self._cfg.admin_channel_id,
                    text=views.test_mode_text(len(self._cfg.test_target_users)),
                )
            except Exception:
                log.exception("시험 모드 알림 실패")


def run_safely(job, now: datetime) -> None:
    try:
        job(now)
    except Exception:
        log.exception("%s 실행 실패", getattr(job, "__name__", job))
