from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from slack_sdk.errors import SlackApiError

from daily_report.db import Store
from daily_report.slack_lists import ListError, ListSync

KST = ZoneInfo("Asia/Seoul")
T1 = datetime(2026, 10, 28, 18, 0, tzinfo=KST)
T2 = datetime(2026, 10, 29, 9, 15, tzinfo=KST)
OCT = date(2026, 10, 28)
NOV = date(2026, 11, 2)


class FakeApi:
    def __init__(self):
        self.calls = []
        self.n = 0
        self.list_items = {}  # list_id -> [item]
        self.fail = {}  # method -> Slack 오류 코드

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method in self.fail:
            # 실제 slack_sdk는 ok:false면 SlackApiError를 던진다
            raise SlackApiError("failed", {"ok": False, "error": self.fail[method]})
        self.n += 1
        if method == "slackLists.create":
            schema = [{**c, "id": f"Col_{c['key']}"} for c in payload["schema"]]
            return {"ok": True, "list_id": f"F{self.n}", "list_metadata": {"schema": schema}}
        if method == "slackLists.items.create":
            return {"ok": True, "item": {"id": f"Rec{self.n}"}}
        if method == "slackLists.items.list":
            items = self.list_items.get(payload["list_id"], [])
            cursor = int(payload.get("cursor") or 0)
            page = items[cursor : cursor + 1]
            nxt = str(cursor + 1) if cursor + 1 < len(items) else ""
            return {"ok": True, "items": page, "response_metadata": {"next_cursor": nxt}}
        if method == "auth.test":
            return {"ok": True, "url": "https://ws.slack.com/", "team_id": "T1"}
        return {"ok": True}

    def methods(self):
        return [m for m, _ in self.calls]

    def last(self, method):
        return [p for m, p in self.calls if m == method][-1]


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def sync(store, api):
    return ListSync(api, store, admin_channel_id="CADMIN")


def add(store, day=OCT, content="API 설계", user="U1"):
    rid = store.add_report(user, day, content, T1)
    return store.get_report_by_id(rid)


def field(payload, key, columns_key="initial_fields"):
    col = f"Col_{key}"
    return next(f for f in payload[columns_key] if f["column_id"] == col)


def test_그_달_첫_보고면_리스트를_만들고_관리자_채널에_보기_권한으로_공유한다(sync, store, api):
    sync.upsert(add(store).id)

    create = api.last("slackLists.create")
    assert create["name"] == "일일보고 2026-10"
    keys = [c["key"] for c in create["schema"]]
    assert keys == ["content", "date", "user", "submitted_at", "updated_at"]
    primary = [c for c in create["schema"] if c.get("is_primary_column")]
    assert [c["key"] for c in primary] == ["content"] and primary[0]["type"] == "text"
    assert api.last("slackLists.access.set") == {
        "list_id": "F1",
        "access_level": "read",
        "channel_ids": ["CADMIN"],
    }
    assert store.get_list_id("2026-10") == "F1"


def test_보고를_행으로_추가한다(sync, store, api):
    report = add(store)

    sync.upsert(report.id)

    item = api.last("slackLists.items.create")
    assert item["list_id"] == "F1"
    assert field(item, "date")["date"] == ["2026-10-28"]
    assert field(item, "user")["user"] == ["U1"]
    content = field(item, "content")["rich_text"][0]["elements"][0]["elements"][0]["text"]
    assert content == "API 설계"
    assert field(item, "submitted_at")["rich_text"][0]["elements"][0]["elements"][0]["text"] == (
        "2026-10-28 18:00"
    )
    assert store.get_list_item(report.id) == ("F1", "Rec3")


def test_같은_달_두_번째_보고는_리스트를_다시_만들지_않는다(sync, store, api):
    sync.upsert(add(store, user="U1").id)
    sync.upsert(add(store, user="U2").id)

    assert api.methods().count("slackLists.create") == 1


def test_보고를_고치면_그_행을_고친다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)
    store.update_report(report.id, "고친 내용", T2)

    sync.upsert(store.get_report_by_id(report.id).id)

    upd = api.last("slackLists.items.update")
    assert upd["list_id"] == "F1"
    assert {c["row_id"] for c in upd["cells"]} == {"Rec3"}
    content = field(upd, "content", "cells")["rich_text"][0]["elements"][0]["elements"][0]["text"]
    assert content == "고친 내용"
    updated = field(upd, "updated_at", "cells")["rich_text"][0]["elements"][0]["elements"][0]
    assert updated["text"] == "2026-10-29 09:15"


def test_달이_바뀌게_옮기면_이전_달_리스트에서_지우고_새_달_리스트에_추가한다(sync, store, api):
    report = add(store, day=date(2026, 10, 30))
    sync.upsert(report.id)
    store.move_report(report.id, NOV, report.content, T2)

    sync.upsert(store.get_report_by_id(report.id).id)

    assert api.last("slackLists.items.delete") == {"list_id": "F1", "id": "Rec3"}
    assert api.last("slackLists.items.create")["list_id"] == store.get_list_id("2026-11")
    assert store.get_list_item(report.id)[0] == store.get_list_id("2026-11")


def test_보고를_지우면_행을_지운다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)
    store.delete_report(report.id)

    sync.remove(report.id)

    assert api.last("slackLists.items.delete") == {"list_id": "F1", "id": "Rec3"}
    assert store.get_list_item(report.id) is None


def test_리스트에_없는_보고를_지우면_아무것도_안_한다(sync, api):
    sync.remove(999)

    assert api.calls == []


def test_동기화는_기간의_행을_DB_기준으로_맞춘다(sync, store, api):
    keep = add(store, day=OCT, user="U1")
    sync.upsert(keep.id)
    store.update_report(keep.id, "DB에서 바뀜", T2)
    new = add(store, day=date(2026, 10, 27), user="U2")  # 리스트에 없던 보고
    # 리스트에는 DB에 없는 행(Rec_orphan)과 기간 밖 행(Rec_out)이 있다
    api.list_items["F1"] = [
        {"id": "Rec3", "fields": [{"column_id": "Col_date", "date": ["2026-10-28"]}]},
        {"id": "Rec_orphan", "fields": [{"column_id": "Col_date", "date": ["2026-10-27"]}]},
        {"id": "Rec_out", "fields": [{"column_id": "Col_date", "date": ["2026-10-01"]}]},
    ]

    result = sync.sync(date(2026, 10, 27), date(2026, 10, 28))

    deleted = [p["id"] for m, p in api.calls if m == "slackLists.items.delete"]
    assert deleted == ["Rec_orphan"]
    assert store.get_list_item(new.id) is not None
    assert result == {"upserted": 2, "deleted": 1, "failed": 0}


def test_동기화_기간이_여러_달이면_달마다_해당_리스트를_맞춘다(sync, store, api):
    sync.upsert(add(store, day=date(2026, 10, 30)).id)
    sync.upsert(add(store, day=NOV).id)

    sync.sync(date(2026, 10, 30), NOV)

    listed = [p["list_id"] for m, p in api.calls if m == "slackLists.items.list"]
    assert sorted(listed) == sorted([store.get_list_id("2026-10"), store.get_list_id("2026-11")])


def test_리스트_링크는_워크스페이스_주소로_만든다(sync, store):
    sync.upsert(add(store).id)

    assert sync.permalink(OCT) == "https://ws.slack.com/lists/T1/F1"


def test_리스트가_없는_달의_링크는_만들지_않고_빈_문자열(sync, api):
    assert sync.permalink(OCT) == ""
    assert "slackLists.create" not in api.methods()


def test_Slack_오류는_ListError로_바꾼다(sync, store, api):
    api.fail["slackLists.create"] = "boom"

    with pytest.raises(ListError, match="boom"):
        sync.upsert(add(store).id)


def test_공유가_실패해도_만든_리스트는_기록하고_다음에는_공유만_다시_한다(sync, store, api):
    api.fail["slackLists.access.set"] = "boom"
    with pytest.raises(ListError):
        sync.ensure_list("2026-10")

    assert store.get_list_id("2026-10") == "F1"  # 만든 리스트를 잃지 않는다

    api.fail.clear()
    assert sync.ensure_list("2026-10") == "F1"
    assert api.methods().count("slackLists.create") == 1
    assert api.methods().count("slackLists.access.set") == 2

    sync.ensure_list("2026-10")
    assert api.methods().count("slackLists.access.set") == 2  # 공유된 뒤에는 다시 안 함


def test_행_수정이_실패하면_새_행을_만들어_다시_연결한다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)
    api.fail["slackLists.items.update"] = "item_not_found"  # 행이 지워졌거나 보관됨

    sync.upsert(report.id)

    assert api.methods().count("slackLists.items.create") == 2
    assert store.get_list_item(report.id) != ("F1", "Rec3")


def test_달이_바뀌면_새_행을_먼저_만들고_옛_행은_나중에_지운다(sync, store, api):
    report = add(store, day=date(2026, 10, 30))
    sync.upsert(report.id)
    store.move_report(report.id, NOV, report.content, T2)

    sync.upsert(store.get_report_by_id(report.id).id)

    methods = api.methods()
    last_create = len(methods) - 1 - methods[::-1].index("slackLists.items.create")
    assert methods.index("slackLists.items.delete") > last_create


def test_옛_행_삭제가_실패해도_옮기기는_끝난다(sync, store, api):
    report = add(store, day=date(2026, 10, 30))
    sync.upsert(report.id)
    store.move_report(report.id, NOV, report.content, T2)
    api.fail["slackLists.items.delete"] = "boom"

    sync.upsert(store.get_report_by_id(report.id).id)

    assert store.get_list_item(report.id)[0] == store.get_list_id("2026-11")


# 2차 리뷰 반영


def test_기록할_때_DB의_최신_보고를_다시_읽는다(sync, store, api):
    report = add(store, content="v1")
    store.update_report(report.id, "v2", T2)

    sync.upsert(report.id)

    item = api.last("slackLists.items.create")
    assert field(item, "content")["rich_text"][0]["elements"][0]["elements"][0]["text"] == "v2"


def test_이미_지운_보고를_늦게_기록하려_하면_리스트_행을_지운다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)
    store.delete_report(report.id)

    sync.upsert(report.id)  # 삭제 전에 시작했다가 늦게 실행된 기록

    assert api.last("slackLists.items.delete") == {"list_id": "F1", "id": "Rec3"}
    assert store.get_list_item(report.id) is None


def test_DB에_보고가_남아_있으면_리스트에서_지우지_않는다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)

    sync.remove(report.id)

    assert "slackLists.items.delete" not in api.methods()


def test_일시적인_오류로_수정이_실패하면_새_행을_만들지_않는다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)
    api.fail["slackLists.items.update"] = "ratelimited"

    with pytest.raises(ListError):
        sync.upsert(report.id)

    assert api.methods().count("slackLists.items.create") == 1


def test_관리자_채널이_바뀌면_그_달_리스트를_새_채널에_다시_공유한다(store, api):
    ListSync(api, store, "COLD").ensure_list("2026-10")

    ListSync(api, store, "CNEW").ensure_list("2026-10")

    shares = [p["channel_ids"] for m, p in api.calls if m == "slackLists.access.set"]
    assert shares == [["COLD"], ["CNEW"]]


def test_Slack에서_리스트가_지워졌으면_그_달_리스트를_새로_만든다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)
    store.update_report(report.id, "v2", T2)
    api.fail["slackLists.items.update"] = "list_not_found"
    api.fail["slackLists.items.create"] = "list_not_found"

    with pytest.raises(ListError):
        sync.upsert(report.id)  # 이번 시도는 실패하고 등록을 지운다

    api.fail.clear()
    sync.upsert(report.id)

    assert store.get_list_id("2026-10") != "F1"
    assert store.get_list_item(report.id)[0] == store.get_list_id("2026-10")


def test_예전_달_리스트에_없는_열은_건너뛴다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)
    columns = store.get_list_columns("F1")
    with store.lock:
        store._db.execute("DELETE FROM list_columns WHERE key = 'updated_at'")

    store.update_report(report.id, "v2", T2)
    sync.upsert(report.id)

    cols = {c["column_id"] for c in api.last("slackLists.items.update")["cells"]}
    assert columns["updated_at"] not in cols


def test_동기화는_보고_하나가_실패해도_나머지와_삭제_단계를_계속한다(sync, store, api):
    ok = add(store, day=OCT, user="U1")
    bad = add(store, day=date(2026, 10, 27), user="U2")
    sync.upsert(ok.id)
    api.list_items["F1"] = [
        {"id": "Rec_orphan", "fields": [{"column_id": "Col_date", "date": ["2026-10-27"]}]}
    ]
    real = api.__call__

    def flaky(method, payload):
        if method == "slackLists.items.create" and payload["initial_fields"][2]["user"] == ["U2"]:
            raise SlackApiError("failed", {"ok": False, "error": "invalid_arguments"})
        return real(method, payload)

    sync._api = flaky
    result = sync.sync(date(2026, 10, 27), OCT)

    assert result["failed"] == 1
    assert result["deleted"] == 1
    assert store.get_list_item(bad.id) is None


def test_기간에_DB_보고가_없는데_리스트_행이_있으면_동기화를_멈춘다(sync, store, api):
    sync.ensure_list("2026-10")
    api.list_items["F1"] = [
        {"id": "Rec1", "fields": [{"column_id": "Col_date", "date": ["2026-10-27"]}]}
    ]

    with pytest.raises(ListError, match="DB"):
        sync.sync(date(2026, 10, 1), date(2026, 10, 31))

    assert "slackLists.items.delete" not in api.methods()


def test_정합성_점검은_지운_보고의_행과_빠진_보고를_맞춘다(sync, store, api):
    gone = add(store, user="U1")
    sync.upsert(gone.id)
    store.delete_report(gone.id)  # 리스트 삭제가 실패해 매핑이 남은 상태
    missing = add(store, user="U2")  # 리스트 기록이 실패해 매핑이 없는 상태

    result = sync.reconcile(since=date(2026, 10, 1))

    assert store.get_list_item(gone.id) is None
    assert store.get_list_item(missing.id) is not None
    assert result == {"upserted": 1, "removed": 1, "failed": 0}


# 3차 리뷰 반영


def test_달을_옮기며_옛_행_삭제가_실패하면_정합성_점검이_다시_지운다(sync, store, api):
    report = add(store, day=date(2026, 10, 30))
    sync.upsert(report.id)
    old = store.get_list_item(report.id)
    store.move_report(report.id, NOV, report.content, T2)
    api.fail["slackLists.items.delete"] = "ratelimited"
    sync.upsert(report.id)
    api.fail.clear()

    result = sync.reconcile(since=date(2026, 10, 1))

    assert api.last("slackLists.items.delete") == {"list_id": old[0], "id": old[1]}
    assert result == {"upserted": 0, "removed": 1, "failed": 0}
    assert sync.reconcile(since=date(2026, 10, 1))["removed"] == 0  # 한 번만 지운다


def test_남은_옛_행이_이미_없으면_지운_것으로_친다(sync, store, api):
    report = add(store, day=date(2026, 10, 30))
    sync.upsert(report.id)
    store.move_report(report.id, NOV, report.content, T2)
    api.fail["slackLists.items.delete"] = "ratelimited"
    sync.upsert(report.id)
    api.fail["slackLists.items.delete"] = "item_not_found"

    assert sync.reconcile(since=date(2026, 10, 1))["failed"] == 0
    api.fail.clear()
    assert sync.reconcile(since=date(2026, 10, 1))["removed"] == 0


def test_비어_있는_수정_시각은_보내지_않는다(sync, store, api):
    sync.upsert(add(store).id)

    cols = [f["column_id"] for f in api.last("slackLists.items.create")["initial_fields"]]
    assert "Col_updated_at" not in cols and "Col_submitted_at" in cols


def test_네트워크_오류도_리스트_오류로_바꿔_실패_건수에_센다(sync, store, api):
    report = add(store)

    def down(method, payload):
        raise ConnectionError("timeout")

    sync._api = down
    with pytest.raises(ListError):
        sync.upsert(report.id)
    assert sync.reconcile(since=date(2026, 10, 1))["failed"] == 1


def test_수정_기록이_실패하면_정합성_점검이_최신_내용으로_고친다(sync, store, api):
    report = add(store)
    sync.upsert(report.id)
    store.update_report(report.id, "고친 내용", T2)
    api.fail["slackLists.items.update"] = "ratelimited"
    with pytest.raises(ListError):
        sync.upsert(report.id)
    api.fail.clear()

    result = sync.reconcile(since=date(2026, 10, 1))

    upd = api.last("slackLists.items.update")
    text = field(upd, "content", "cells")["rich_text"][0]["elements"][0]["elements"][0]["text"]
    assert text == "고친 내용" and result["upserted"] == 1
    assert sync.reconcile(since=date(2026, 10, 1))["upserted"] == 0  # 맞춘 뒤에는 다시 안 함


def test_달을_옮기며_새_행_만들기가_실패하면_정합성_점검이_옮긴다(sync, store, api):
    report = add(store, day=date(2026, 10, 30))
    sync.upsert(report.id)
    store.move_report(report.id, NOV, report.content, T2)
    api.fail["slackLists.items.create"] = "ratelimited"
    with pytest.raises(ListError):
        sync.upsert(report.id)
    api.fail.clear()

    sync.reconcile(since=date(2026, 10, 1))

    assert store.get_list_item(report.id)[0] == store.get_list_id("2026-11")
