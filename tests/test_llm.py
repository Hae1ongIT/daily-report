import json
from datetime import date
from types import SimpleNamespace

from daily_report.llm import Summarizer, openai_complete
from daily_report.summary import PersonReports, SummaryData

FRI = date(2026, 10, 30)
SAT = date(2026, 10, 31)


def data(people):
    return SummaryData(
        workday=FRI,
        rest_days=[SAT],
        days=[FRI, SAT],
        submitted=len(people),
        total=len(people),
        missing=[],
        annual=[],
        half_missing=[],
        submitted_on_leave=[],
        rest_reports=[],
        people=people,
    )


PEOPLE = [
    PersonReports("U1", "길동", [(FRI, "API 설계 마무리"), (SAT, "장애 대응")]),
    PersonReports("U2", "철수", [(FRI, "배포 준비")]),
]

GOOD = json.dumps(
    {
        "team": "API 설계를 마치고 배포를 준비했다.",
        "people": [
            {"id": "P1", "summary": "(금) API 설계 마무리 / (토) 장애 대응"},
            {"id": "P2", "summary": "(금) 배포 준비"},
        ],
    },
    ensure_ascii=False,
)


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make(llm, retries=3):
    sleeps = []
    return Summarizer(llm, retries, 60, sleep=sleeps.append), sleeps


def test_팀_요약과_사람별_요약을_돌려준다():
    llm = FakeLLM(GOOD)
    s, _ = make(llm)

    out = s.summarize(data(PEOPLE))

    assert out.team == "API 설계를 마치고 배포를 준비했다."
    assert out.people == [
        ("길동", "(금) API 설계 마무리 / (토) 장애 대응"),
        ("철수", "(금) 배포 준비"),
    ]


def test_프롬프트에_이름_날짜_요일_내용이_들어간다():
    llm = FakeLLM(GOOD)
    s, _ = make(llm)

    s.summarize(data(PEOPLE))

    _, user = llm.calls[0]
    assert "[P1] 길동" in user and "[P2] 철수" in user
    assert "10/30(금) API 설계 마무리" in user and "10/31(토) 장애 대응" in user


def test_실패하면_정해진_간격으로_다시_시도한다():
    llm = FakeLLM(TimeoutError(), ConnectionError(), GOOD)
    s, sleeps = make(llm)

    out = s.summarize(data(PEOPLE))

    assert out is not None
    assert len(llm.calls) == 3
    assert sleeps == [60, 60]


def test_재시도까지_모두_실패하면_None():
    llm = FakeLLM(*[TimeoutError()] * 4)
    s, sleeps = make(llm, retries=3)

    assert s.summarize(data(PEOPLE)) is None
    assert len(llm.calls) == 4  # 처음 1번 + 다시 시도 3번
    assert sleeps == [60, 60, 60]


def test_응답_형식이_틀리면_실패로_보고_다시_시도한다():
    llm = FakeLLM("요약입니다", GOOD)
    s, _ = make(llm)

    assert s.summarize(data(PEOPLE)) is not None
    assert len(llm.calls) == 2


def test_코드블록으로_감싼_JSON도_읽는다():
    llm = FakeLLM(f"```json\n{GOOD}\n```")
    s, _ = make(llm)

    assert s.summarize(data(PEOPLE)).team.startswith("API")


def test_LLM이_빠뜨린_사람은_요약_없음으로_채운다():
    partial = json.dumps({"team": "t", "people": [{"id": "P1", "summary": "x"}]})
    s, _ = make(FakeLLM(partial))

    out = s.summarize(data(PEOPLE))

    assert out.people == [("길동", "x"), ("철수", "(요약 없음)")]


def test_보고가_없으면_LLM을_부르지_않는다():
    llm = FakeLLM()
    s, _ = make(llm)

    out = s.summarize(data([]))

    assert out.team == "" and out.people == []
    assert llm.calls == []


def test_openai_호환_API로_부른다():
    seen = {}

    def create(**kw):
        seen.update(kw)
        msg = SimpleNamespace(content="답")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    complete = openai_complete(client, "my-model")

    assert complete("sys", "usr") == "답"
    assert seen["model"] == "my-model"
    assert seen["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]


def test_이름이_같아도_번호로_구분해_매칭한다():
    same = [PersonReports("U1", "민수", [(FRI, "a")]), PersonReports("U2", "민수", [(FRI, "b")])]
    reply = json.dumps(
        {
            "team": "t",
            "people": [{"id": "P2", "summary": "b요약"}, {"id": "P1", "summary": "a요약"}],
        }
    )
    s, _ = make(FakeLLM(reply))

    assert s.summarize(data(same)).people == [("민수", "a요약"), ("민수", "b요약")]


def test_추론_모델의_think_블록은_건너뛰고_JSON만_읽는다():
    reply = "<think>{이건 생각} 어떻게 쓸까</think>\n" + GOOD + "\n끝"
    s, _ = make(FakeLLM(reply))

    assert s.summarize(data(PEOPLE)).team.startswith("API")


def test_번호에_대괄호나_숫자만_와도_사람과_맞춘다():
    loose = json.dumps(
        {"team": "t", "people": [{"id": "[P1]", "summary": "a"}, {"id": 2, "summary": "b"}]}
    )
    s, _ = make(FakeLLM(loose))

    assert s.summarize(data(PEOPLE)).people == [("길동", "a"), ("철수", "b")]


def test_사람별_요약이_하나도_맞지_않으면_실패로_보고_다시_시도한다():
    wrong = json.dumps({"team": "t", "people": [{"id": "X", "summary": "a"}]})
    s, sleeps = make(FakeLLM(wrong, GOOD))

    out = s.summarize(data(PEOPLE))

    assert out.people[0] == ("길동", "(금) API 설계 마무리 / (토) 장애 대응")
    assert sleeps == [60]


def test_종료하면_재시도를_멈추고_실패로_올리지_않는다():
    import pytest

    from daily_report.llm import Stopped

    s, _ = make(FakeLLM(RuntimeError("down"), GOOD))
    s.cancel()

    with pytest.raises(Stopped):
        s.summarize(data(PEOPLE))


def test_재시도_대기는_종료_신호로_바로_끝난다():
    import threading
    import time

    llm = FakeLLM(RuntimeError("down"), GOOD)
    s = Summarizer(llm, 3, 60)
    threading.Timer(0.1, s.cancel).start()
    started = time.monotonic()

    try:
        s.summarize(data(PEOPLE))
    except Exception:
        pass

    assert time.monotonic() - started < 5
