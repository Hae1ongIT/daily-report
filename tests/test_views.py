import json
from datetime import date, time, timedelta

from daily_report import views
from daily_report.summary import LlmSummary, PersonReports, SummaryData

WED = date(2026, 10, 28)
FRI = date(2026, 10, 30)
SAT = date(2026, 10, 31)


def all_text(blocks) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def action_ids(blocks):
    ids = []
    for b in blocks:
        if b.get("accessory"):
            ids.append(b["accessory"].get("action_id"))
        for e in b.get("elements", []):
            if "action_id" in e:
                ids.append(e["action_id"])
    return ids


# 보고 모달


def test_보고_모달은_날짜와_업무_내용을_받는다():
    v = views.report_modal(WED)

    assert v["callback_id"] == views.REPORT_SUBMIT
    date_block = next(b for b in v["blocks"] if b["block_id"] == "date")
    content_block = next(b for b in v["blocks"] if b["block_id"] == "content")
    assert date_block["element"]["initial_date"] == "2026-10-28"
    assert content_block["element"]["multiline"] is True
    assert content_block["element"]["max_length"] == 3000
    assert json.loads(v["private_metadata"]) == {"existing_id": None}


def test_기존_보고를_불러오면_내용과_보고_ID를_담는다():
    v = views.report_modal(WED, content="원래 내용", existing_id=7)

    content_block = next(b for b in v["blocks"] if b["block_id"] == "content")
    assert content_block["element"]["initial_value"] == "원래 내용"
    assert json.loads(v["private_metadata"]) == {"existing_id": 7}
    assert "수정" in v["title"]["text"]


# DM


def test_보고_요청_DM_버튼은_그_날짜를_담는다():
    blocks = views.request_dm(WED)

    button = next(e for b in blocks for e in b.get("elements", []) if e.get("type") == "button")
    assert button["action_id"] == views.OPEN_REPORT
    assert button["value"] == "2026-10-28"


def test_재알림_DM은_어제_보고를_요청한다():
    blocks = views.reminder_dm(FRI, time(10, 0))

    assert "10/30(금)" in all_text(blocks)
    button = next(e for b in blocks for e in b.get("elements", []) if e.get("type") == "button")
    assert button["value"] == "2026-10-30"


# 앱 홈


ROWS = [
    views.HomeRow(WED, "submitted", date(2026, 11, 4), report_id=3, preview="API 설계"),
    views.HomeRow(date(2026, 10, 27), "missing", date(2026, 11, 3)),
    views.HomeRow(date(2026, 10, 26), "annual", date(2026, 11, 2)),
    views.HomeRow(SAT, "submitted", date(2026, 11, 6), report_id=4, preview="장애"),
]


def test_보고_대상자의_앱_홈에는_보고_영역이_있다():
    v = views.app_home(is_target=True, is_admin=False, rows=ROWS)
    ids = action_ids(v["blocks"])
    text = all_text(v["blocks"])

    assert v["type"] == "home"
    assert views.OPEN_REPORT in ids and views.OPEN_LEAVE in ids and views.OPEN_LEAVE_CANCEL in ids
    assert "11/4(수)까지 수정 가능" in text
    assert "연차" in text
    assert views.ADMIN_EXCLUSIONS not in ids


def test_날짜별로_수정_삭제_작성_버튼을_둔다():
    v = views.app_home(is_target=True, is_admin=False, rows=ROWS)
    buttons = [e for b in v["blocks"] for e in b.get("elements", []) if e.get("type") == "button"]

    edit = [b for b in buttons if b["action_id"] == views.EDIT_REPORT]
    delete = [b for b in buttons if b["action_id"] == views.DELETE_REPORT]
    write = [b for b in buttons if b["action_id"] == views.WRITE_REPORT]
    assert [b["value"] for b in edit] == ["3", "4"]
    assert all("confirm" in b for b in delete)  # 삭제 확인 창
    assert [b["value"] for b in write] == ["2026-10-27"]


def test_대상이_아니면_안내만_보여준다():
    v = views.app_home(is_target=False, is_admin=False, rows=[])

    assert "보고 대상이 아니에요" in all_text(v["blocks"])
    assert action_ids(v["blocks"]) == []


def test_제외_명단에_있는_관리자에게는_안내와_관리_메뉴가_함께_보인다():
    v = views.app_home(is_target=False, is_admin=True, rows=[])
    ids = action_ids(v["blocks"])

    assert "보고 대상이 아니에요" in all_text(v["blocks"])
    assert {
        views.ADMIN_EXCLUSIONS,
        views.ADMIN_CALENDAR,
        views.ADMIN_SYNC,
        views.ADMIN_LEAVE,
    } <= set(ids)
    assert views.OPEN_REPORT not in ids


# 연차·관리 모달


def test_연차_등록_모달은_종류와_기간을_받는다():
    v = views.leave_modal(register=True, for_other=False)
    ids = {b["block_id"] for b in v["blocks"] if "block_id" in b}

    assert v["callback_id"] == views.LEAVE_SUBMIT
    assert {"kind", "start", "end"} <= ids
    assert "user" not in ids


def test_관리자가_다른_사람_연차를_다룰_때는_사람을_고른다():
    v = views.leave_modal(register=False, for_other=True)
    ids = {b["block_id"] for b in v["blocks"]}

    assert "user" in ids and "kind" not in ids
    assert json.loads(v["private_metadata"]) == {"register": False, "for_other": True}


def test_제외_명단_모달은_현재_명단을_채워_둔다():
    v = views.exclusions_modal(["U1", "U2"])

    el = v["blocks"][0]["element"]
    assert el["type"] == "multi_users_select" and el["initial_users"] == ["U1", "U2"]


def test_휴무일_모달은_추가_출근일_취소를_고른다():
    v = views.calendar_modal()
    kind = next(b for b in v["blocks"] if b.get("block_id") == "kind")

    values = {o["value"] for o in kind["element"]["options"]}
    assert values == {"off", "work", "remove_off", "remove_work"}


def test_동기화_모달은_기간을_받는다():
    v = views.sync_modal()

    assert {b["block_id"] for b in v["blocks"] if "block_id" in b} == {"start", "end"}
    assert "지워져요" in v["blocks"][0]["text"]["text"]  # 직접 넣은 행도 지운다는 안내


# 요약 메시지


def summary_data(**kw):
    base = dict(
        workday=FRI,
        rest_days=[SAT, date(2026, 11, 1)],
        days=[FRI, SAT, date(2026, 11, 1)],
        submitted=7,
        total=9,
        missing=["a", "b"],
        annual=["c"],
        half_missing=["d"],
        submitted_on_leave=["e"],
        rest_reports=[("a", SAT)],
        people=[PersonReports("A", "a", [(FRI, "x"), (SAT, "y")])],
    )
    base.update(kw)
    return SummaryData(**base)


def test_요약_메시지에_제출_현황과_LLM_요약과_리스트_링크가_들어간다():
    llm = LlmSummary("팀이 일했다", [("a", "(금) x / (토) y")])

    blocks, text = views.summary_message(summary_data(), llm, [("10월", "https://list")])
    body = all_text(blocks)

    assert "10/30(금)" in text
    assert "제출 7/9" in body
    assert "미제출: a, b" in body and "연차: c" in body and "반차(미제출): d" in body
    assert "제출(연차): e" in body
    assert "쉬는 날 보고: a(토)" in body
    assert "팀이 일했다" in body
    assert "(금) x / (토) y" in body
    assert "https://list" in body


def test_사람별_요약은_표_블록으로_넣는다():
    llm = LlmSummary("t", [("a", "s")])

    blocks, _ = views.summary_message(summary_data(), llm, [])

    table = next(b for b in blocks if b["type"] == "table")
    assert table["rows"][1][0]["text"] == "a"


def test_LLM_실패면_실패_표시와_제출_현황만_넣는다():
    blocks, _ = views.summary_message(summary_data(), None, [("10월", "https://list")])
    body = all_text(blocks)

    assert "LLM 요약 실패" in body
    assert "제출 7/9" in body and "https://list" in body
    assert not any(b["type"] == "table" for b in blocks)


def test_요약에는_멘션을_쓰지_않고_특수문자를_이스케이프한다():
    data = summary_data(missing=["<@U1>", "a&b"], people=[])
    blocks, _ = views.summary_message(data, LlmSummary("", []), [])
    body = all_text(blocks)

    assert "<@U1>" not in body
    assert "&lt;@U1&gt;" in body and "a&amp;b" in body


def test_요약을_놓쳤을_때_알림():
    assert "요약을 놓쳤어요" in views.missed_summary_text([FRI])


def test_시험_모드_알림():
    assert "시험 모드: 2명 대상" in views.test_mode_text(2)


def test_보고_없는_반차일은_반차로_표시하고_작성_버튼을_둔다():
    row = views.HomeRow(date(2026, 10, 27), "half", date(2026, 11, 3))
    v = views.app_home(is_target=True, is_admin=False, rows=[row])
    text = all_text(v["blocks"])

    assert "반차" in text
    assert views.WRITE_REPORT in action_ids(v["blocks"])


def test_리스트_링크가_없으면_링크를_넣지_않는다():
    blocks, _ = views.summary_message(summary_data(), LlmSummary("t", []), [])

    assert "<|" not in all_text(blocks)


def test_연차_행에도_수정_가능_기한을_표시한다():
    row = views.HomeRow(date(2026, 10, 26), "annual", date(2026, 11, 2))

    assert "11/2(월)까지 수정 가능" in all_text(views.app_home(True, False, [row])["blocks"])


def test_제외_명단이_비어_있으면_initial_users를_넣지_않는다():
    el = views.exclusions_modal([])["blocks"][0]["element"]

    assert "initial_users" not in el


def test_관리_메뉴에_다른_사람_연차_취소가_있다():
    assert views.ADMIN_LEAVE_CANCEL in action_ids(views.app_home(False, True, [])["blocks"])


def test_LLM_요약이_길면_블록_한도_안으로_자른다():
    llm = LlmSummary("가" * 5000, [("a", "나" * 2000)])

    blocks, _ = views.summary_message(summary_data(), llm, [])

    team = next(b for b in blocks if "팀 요약" in b.get("text", {}).get("text", ""))
    assert len(team["text"]["text"]) <= 3000
    table = next(b for b in blocks if b["type"] == "table")
    assert sum(len(c["text"]) for row in table["rows"] for c in row) <= 10000


def test_사람이_많으면_표는_100행을_넘지_않는다():
    llm = LlmSummary("t", [(f"p{i}", "s") for i in range(150)])

    blocks, _ = views.summary_message(summary_data(), llm, [])

    table = next(b for b in blocks if b["type"] == "table")
    assert len(table["rows"]) <= 100
    assert "외 51명" in all_text(blocks)


def test_근무일_없이_쉬는_날_보고만_남은_요약():
    data = summary_data(
        workday=None,
        rest_days=[date(2026, 10, 27)],
        days=[date(2026, 10, 27)],
        submitted=0,
        total=0,
        missing=[],
        annual=[],
        half_missing=[],
        submitted_on_leave=[],
        rest_reports=[("b", date(2026, 10, 27))],
    )

    blocks, title = views.summary_message(data, LlmSummary("t", [("b", "s")]), [])

    assert "10/27(화)" in title
    assert "제출 0/0" not in all_text(blocks)
    assert "쉬는 날 보고: b(화)" in all_text(blocks)


def test_같은_사람의_쉬는_날_보고는_이름을_한_번만_쓴다():
    data = summary_data(rest_reports=[("a", SAT), ("a", date(2026, 11, 1)), ("b", SAT)])

    blocks, _ = views.summary_message(data, LlmSummary("t", []), [])

    assert "쉬는 날 보고: a(토·일), b(토)" in all_text(blocks)


def test_요약에_걸친_달마다_리스트_링크를_넣는다():
    links = [("10월", "https://l/10"), ("11월", "https://l/11")]

    blocks, _ = views.summary_message(summary_data(), LlmSummary("t", []), links)

    body = all_text(blocks)
    assert "<https://l/10|원본 보고 리스트(10월)>" in body
    assert "<https://l/11|원본 보고 리스트(11월)>" in body


def test_DM_미리보기_문구에_날짜를_넣는다():
    assert "10/30(금)" in views.request_text(FRI)
    assert "10/30(금)" in views.reminder_text(FRI, time(10, 0))


def test_여러_날의_놓친_요약을_한_문구로_알린다():
    text = views.missed_summary_text([date(2026, 10, 27), date(2026, 10, 28)])

    assert "10/27(화)" in text and "10/28(수)" in text


def test_보고_요청_DM을_다_보내지_못한_날이면_요약에_알린다():
    blocks, _ = views.summary_message(summary_data(request_missed=True), None, [])
    assert "보고 요청 DM을 다 보내지 못했어요" in all_text(blocks)

    blocks, _ = views.summary_message(summary_data(), None, [])
    assert "보고 요청 DM" not in all_text(blocks)


def test_팀_요약은_이스케이프한_뒤의_길이로_자른다():
    blocks, _ = views.summary_message(summary_data(), LlmSummary("&" * 2500, [("a", "s")]), [])

    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")


# 3차 리뷰(화면·문구) 반영


def test_창의_닫기_버튼은_닫기이고_연차_취소_창의_제출_버튼은_취소_동작을_말한다():
    cancel = views.leave_modal(register=False, for_other=False)

    assert cancel["close"]["text"] == "닫기"
    assert cancel["submit"]["text"] == "연차·반차 취소"
    assert views.leave_modal(register=True, for_other=False)["submit"]["text"] == "등록"


def test_수정_창에는_날짜를_바꾸면_보고가_옮겨진다고_알린다():
    edit = views.report_modal(FRI, "x", existing_id=3)
    new = views.report_modal(FRI)

    assert "옮겨" in json.dumps(edit, ensure_ascii=False)
    assert "옮겨" not in json.dumps(new, ensure_ascii=False)


def test_앱_홈_미리보기는_서식_없이_한_줄로_줄이고_잘리면_말줄임표를_붙인다():
    from daily_report.home import HomeRow

    row = HomeRow(FRI, "submitted", date(2026, 11, 6), 1, "*굵게*\n" + "가" * 100, False)
    blocks = views.app_home(True, False, [row])["blocks"]

    plain = [
        b["text"] for b in blocks if b["type"] == "section" and b["text"]["type"] == "plain_text"
    ]
    assert len(plain) == 1 and plain[0]["emoji"] is False
    assert plain[0]["text"].startswith("*굵게* 가") and plain[0]["text"].endswith("…")
    assert len(plain[0]["text"]) == 80


def test_앱_홈_수정_기한에_요일을_붙인다():
    from daily_report.home import HomeRow

    row = HomeRow(FRI, "missing", date(2026, 11, 6), None, "", False)

    assert "11/6(금)까지 수정 가능" in all_text(views.app_home(True, False, [row])["blocks"])


def test_재알림에는_요약_시각을_적는다():
    from datetime import time

    assert "10:00 요약 전에" in views.reminder_text(FRI, time(10, 0))


def test_요약_알림_문구에_제출_현황을_넣는다():
    _, text = views.summary_message(summary_data(), None, [])

    assert "제출 7/9" in text


def test_휴무일_출근일_창에_출근일_지정_취소가_있다():
    kind = next(b for b in views.calendar_modal()["blocks"] if b.get("block_id") == "kind")

    assert "remove_work" in [o["value"] for o in kind["element"]["options"]]


def test_앱_홈에_예정된_연차_반차를_한_줄로_보여준다():
    upcoming = [(FRI, date(2026, 11, 2), "annual"), (date(2026, 11, 3), date(2026, 11, 3), "half")]

    text = all_text(views.app_home(True, False, [], upcoming)["blocks"])

    assert "예정된 연차·반차: 10/30(금)~11/2(월) 연차, 11/3(화) 반차" in text


def test_예정된_연차가_없으면_줄을_넣지_않는다():
    assert "예정된" not in all_text(views.app_home(True, False, [])["blocks"])


def test_예정된_연차가_많으면_앞의_10개만_보여준다():
    days = [date(2026, 11, d) for d in range(2, 30)]
    upcoming = [(d, d, "annual") for d in days[:12]]

    text = all_text(views.app_home(True, False, [], upcoming)["blocks"])

    assert "외 2건" in text


def test_휴무일_모달_위에_지정한_휴무일과_출근일을_보여준다():
    v = views.calendar_modal(
        off_days=[date(2026, 11, 2), date(2026, 11, 3)], work_days=[date(2026, 10, 31)]
    )

    text = v["blocks"][0]["text"]["text"]
    assert "추가한 휴무일: 11/2(월), 11/3(화)" in text
    assert "지정한 출근일: 10/31(토)" in text


def test_휴무일_모달은_지정한_날이_없으면_없다고_알린다():
    text = views.calendar_modal()["blocks"][0]["text"]["text"]

    assert "추가한 휴무일: 없음" in text and "지정한 출근일: 없음" in text


def test_휴무일_모달의_지정일_목록은_길면_줄인다():
    days = [date(2026, 11, 1) + timedelta(days=i) for i in range(30)]

    text = views.calendar_modal(off_days=days)["blocks"][0]["text"]["text"]

    assert f"외 {30 - views.OVERRIDES_MAX}건" in text


def test_관리자_앱_홈_관리_메뉴에_지정한_휴무일과_출근일을_보여준다():
    v = views.app_home(
        is_target=False,
        is_admin=True,
        rows=[],
        off_days=[date(2026, 10, 30)],
        work_days=[date(2026, 10, 31)],
    )
    texts = [b["text"]["text"] for b in v["blocks"] if b["type"] == "section"]

    assert any("추가한 휴무일: 10/30(금)" in t and "지정한 출근일: 10/31(토)" in t for t in texts)


def test_관리자가_아니면_휴무일_출근일_목록이_없다():
    v = views.app_home(is_target=True, is_admin=False, rows=[], off_days=[date(2026, 10, 30)])

    assert "추가한 휴무일" not in json.dumps(v, ensure_ascii=False)


def test_본인_연차_창_위에_예정된_연차_반차를_보여준다():
    upcoming = [(date(2026, 11, 3), date(2026, 11, 5), "annual")]
    v = views.leave_modal(register=False, for_other=False, upcoming=upcoming)

    assert v["blocks"][0]["text"]["text"] == "예정된 연차·반차: 11/3(화)~11/5(목) 연차"


def test_본인_연차_창은_예정이_없으면_없음으로_보여준다():
    v = views.leave_modal(register=True, for_other=False)

    assert v["blocks"][0]["text"]["text"] == "예정된 연차·반차: 없음"


def test_다른_사람_연차_창에는_본인_예정을_보여주지_않는다():
    v = views.leave_modal(register=True, for_other=True)

    assert "예정된" not in json.dumps(v, ensure_ascii=False)


def test_앱_홈은_예정된_연차_반차를_버튼보다_위에_둔다():
    upcoming = [(date(2026, 11, 3), date(2026, 11, 3), "annual")]
    blocks = views.app_home(is_target=True, is_admin=False, rows=[], upcoming=upcoming)["blocks"]

    info = next(i for i, b in enumerate(blocks) if "예정된" in json.dumps(b, ensure_ascii=False))
    buttons = next(i for i, b in enumerate(blocks) if b["type"] == "actions")
    assert info < buttons


def test_앱_홈_맨_위에는_제목이_없고_관리_메뉴만_제목을_쓴다():
    v = views.app_home(is_target=True, is_admin=True, rows=[])
    headers = [b["text"]["text"] for b in v["blocks"] if b["type"] == "header"]

    assert headers == ["관리 메뉴"]


def test_관리_메뉴에_연차_반차_현황_버튼이_있다():
    assert views.ADMIN_LEAVE_STATUS in action_ids(views.app_home(False, True, [])["blocks"])


def test_연차_현황_창은_사람별로_한_줄씩_보여주고_제출_버튼이_없다():
    people = [
        ("홍길동", [(date(2026, 11, 3), date(2026, 11, 5), "annual")]),
        ("김철수", [(date(2026, 11, 9), date(2026, 11, 9), "half")]),
    ]
    v = views.leave_status_modal(date(2026, 10, 29), date(2026, 11, 25), people)
    text = json.dumps(v, ensure_ascii=False)

    assert "submit" not in v
    assert "10/29(목)~11/25(수)" in text
    assert "*홍길동*: 11/3(화)~11/5(목) 연차" in text
    assert "*김철수*: 11/9(월) 반차" in text


def test_연차_현황_창은_없으면_없다고_알린다():
    v = views.leave_status_modal(date(2026, 10, 29), date(2026, 11, 25), [])

    assert "등록된 연차·반차가 없어요" in json.dumps(v, ensure_ascii=False)


def test_연차_현황_창은_사람이_많아도_블록_한도를_넘지_않는다():
    groups = [(date(2026, 11, d), date(2026, 11, d), "half") for d in range(2, 12)]
    people = [(f"사람{i:03d}", groups) for i in range(300)]

    v = views.leave_status_modal(date(2026, 10, 29), date(2026, 11, 25), people)

    assert len(v["blocks"]) <= 100
    assert all(len(b["text"]["text"]) <= 3000 for b in v["blocks"] if b["type"] == "section")


def test_연차_현황_창이_한도를_넘으면_남은_사람_수를_알린다(monkeypatch):
    monkeypatch.setattr(views, "STATUS_SECTION_MAX", 30)
    monkeypatch.setattr(views, "STATUS_BLOCKS_MAX", 3)
    people = [(f"p{i}", [(date(2026, 11, 2), date(2026, 11, 2), "half")]) for i in range(10)]

    v = views.leave_status_modal(date(2026, 10, 29), date(2026, 11, 25), people)
    texts = [b["text"]["text"] for b in v["blocks"]]

    shown = sum(t.count("*p") for t in texts)
    more = int(texts[-1].removeprefix("외 ").removesuffix("명"))
    assert texts[-1].startswith("외 ") and shown + more == 10
