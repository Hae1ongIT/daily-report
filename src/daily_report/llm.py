"""사내 vLLM(OpenAI 호환 API)으로 팀 요약·사람별 요약을 만든다. 외부 LLM은 쓰지 않는다."""

import json
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

from daily_report.fmt import day_label
from daily_report.summary import LlmSummary, SummaryData

log = logging.getLogger(__name__)

Complete = Callable[[str, str], str]

SYSTEM_PROMPT = """너는 팀 일일보고를 관리자에게 요약해 주는 도우미다.
입력은 사람별 업무 보고다.
반드시 아래 JSON 하나만 출력한다. 다른 말은 쓰지 않는다.
{"team": "팀 전체가 한 일을 2~4문장으로 요약",
 "people": [{"id": "입력의 사람 번호(예: P1)", "summary": "그 사람이 한 일을 한두 줄로"}]}
사람별 요약에서 그 사람의 보고 날짜가 여러 개일 때만 (금) ... / (토) ... 처럼 요일을 붙인다.
날짜가 하나면 날짜와 요일을 쓰지 않는다.
"""


class Stopped(RuntimeError):
    """봇을 끄는 중이라 요약을 멈췄다. "LLM 요약 실패"로 올리지 않고 다음 실행에서 다시 한다."""


class Summarizer:
    def __init__(
        self,
        complete: Complete,
        retry_count: int,
        retry_interval_seconds: int,
        sleep: Callable[[float], object] | None = None,
    ):
        self._complete = complete
        self._retry_count = retry_count
        self._interval = retry_interval_seconds
        self._stop = threading.Event()
        # 재시도 대기는 종료 신호로 바로 깨어난다(그러지 않으면 봇이 몇 분 동안 꺼지지 않는다)
        self._sleep = sleep or self._stop.wait

    def cancel(self) -> None:
        self._stop.set()

    def summarize(self, data: SummaryData) -> LlmSummary | None:
        """실패하면 정해진 횟수만큼 다시 시도하고, 그래도 안 되면 None."""
        if not data.people:
            return LlmSummary("", [])
        prompt = _user_prompt(data)
        for attempt in range(self._retry_count + 1):
            if attempt:
                self._sleep(self._interval)
            if self._stop.is_set():
                raise Stopped("봇을 끄는 중이라 요약을 멈춤")
            try:
                return _parse(self._complete(SYSTEM_PROMPT, prompt), data)
            except Exception as e:  # 네트워크 오류, 형식 오류 모두 다시 시도
                log.warning("LLM 요약 실패 (%d번째): %s", attempt + 1, e)
        return None


def openai_complete(client: Any, model: str) -> Complete:
    def complete(system: str, user: str) -> str:
        res = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        return res.choices[0].message.content or ""

    return complete


def _user_prompt(data: SummaryData) -> str:
    scope = ", ".join(day_label(d) for d in data.days)
    lines = [f"요약 대상 날짜: {scope}"]
    for i, person in enumerate(data.people, 1):
        lines.append(f"\n[P{i}] {person.name}")
        lines.extend(f"- {day_label(day)} {content}" for day, content in person.entries)
    return "\n".join(lines)


def _parse(text: str, data: SummaryData) -> LlmSummary:
    # 추론 모델의 <think> 블록을 건너뛰고, 그 뒤 첫 JSON 객체만 읽는다
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    start = text.find("{")
    if start < 0:
        raise ValueError("JSON을 찾을 수 없음")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    team = str(obj["team"]).strip()
    given = {}
    for p in obj["people"]:
        number = re.search(r"\d+", str(p["id"]))  # "P1", "[P1]", 1 모두 받는다
        if number:
            given[int(number.group())] = str(p["summary"]).strip()
    people = []
    for i, person in enumerate(data.people, 1):
        summary = given.get(i)
        if not summary:
            log.warning("LLM이 사람별 요약을 빠뜨림: P%d", i)
        people.append((person.name, summary or "(요약 없음)"))
    if data.people and not any(given.get(i) for i in range(1, len(data.people) + 1)):
        raise ValueError("사람별 요약의 번호가 하나도 맞지 않음")
    return LlmSummary(team, people)
