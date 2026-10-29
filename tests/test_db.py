import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from daily_report.db import ANNUAL, HALF, Store

KST = ZoneInfo("Asia/Seoul")
D1 = date(2026, 10, 27)
D2 = date(2026, 10, 28)
T1 = datetime(2026, 10, 27, 18, 0, tzinfo=KST)
T2 = datetime(2026, 10, 28, 9, 30, tzinfo=KST)


@pytest.fixture
def store():
    return Store(":memory:")


def test_보고를_저장하고_조회한다(store):
    rid = store.add_report("U1", D1, "API 설계", T1)

    r = store.get_report("U1", D1)
    assert r.id == rid
    assert r.content == "API 설계"
    assert r.submitted_at == T1
    assert r.updated_at is None


def test_한_사람의_같은_날짜_보고는_하나뿐이다(store):
    store.add_report("U1", D1, "a", T1)

    with pytest.raises(sqlite3.IntegrityError):
        store.add_report("U1", D1, "b", T1)


def test_보고를_고치면_수정_시각이_남는다(store):
    rid = store.add_report("U1", D1, "a", T1)

    store.update_report(rid, "b", T2)

    r = store.get_report_by_id(rid)
    assert r.content == "b"
    assert r.updated_at == T2


def test_보고를_다른_날짜로_옮긴다(store):
    rid = store.add_report("U1", D1, "a", T1)

    store.move_report(rid, D2, "a2", T2)

    assert store.get_report("U1", D1) is None
    moved = store.get_report("U1", D2)
    assert moved.id == rid and moved.content == "a2" and moved.updated_at == T2


def test_보고를_지운다(store):
    rid = store.add_report("U1", D1, "a", T1)

    store.delete_report(rid)

    assert store.get_report_by_id(rid) is None


def test_날짜들의_보고를_모은다(store):
    store.add_report("U1", D1, "a", T1)
    store.add_report("U2", D2, "b", T1)
    store.add_report("U3", date(2026, 10, 29), "c", T1)

    found = store.reports_on([D1, D2])

    assert {(r.user_id, r.report_date) for r in found} == {("U1", D1), ("U2", D2)}


def test_연차_반차를_등록하고_지운다(store):
    store.add_leaves("U1", [D1, D2], ANNUAL)
    store.add_leaves("U2", [D2], HALF)

    assert store.leaves_on(D2) == {"U1": ANNUAL, "U2": HALF}
    assert store.get_leave("U1", D1) == ANNUAL

    store.delete_leaves("U1", [D1])

    assert store.get_leave("U1", D1) is None


def test_제외_명단을_관리한다(store):
    store.add_exclusion("U1")
    store.add_exclusion("U2")
    store.remove_exclusion("U2")

    assert store.exclusions() == {"U1"}


def test_휴무일과_출근일_지정을_관리한다(store):
    store.set_override(D1, "off")
    store.set_override(D1, "work")  # 덮어쓴다
    store.set_override(D2, "off")
    store.remove_override(D2)

    assert store.overrides() == {D1: "work"}


def test_날짜별_대상자_목록을_저장하고_지운다(store):
    assert store.get_targets(D1) is None  # 목록이 아직 없음

    store.save_targets(D1, {"U1", "U2"})
    store.save_targets(D2, set())  # 빈 목록도 "목록 있음"이다

    assert store.get_targets(D1) == {"U1", "U2"}
    assert store.get_targets(D2) == set()

    store.delete_targets(D1)

    assert store.get_targets(D1) is None


def test_예약_작업은_같은_날짜에_한_번만_기록된다(store):
    assert store.mark_job_run("summary", D1, T2) is True
    assert store.mark_job_run("summary", D1, T2) is False
    assert store.has_job_run("summary", D1)
    assert not store.has_job_run("summary", D2)


def test_월별_리스트와_리스트_행을_기록한다(store):
    store.register_list("2026-10", "F1", {"date": "Col1"})
    rid = store.add_report("U1", D1, "a", T1)
    store.set_list_item(rid, "F1", "I1")

    assert store.get_list_id("2026-10") == "F1"
    assert store.get_list_id("2026-11") is None
    assert store.get_list_item(rid) == ("F1", "I1")

    store.delete_list_item(rid)

    assert store.get_list_item(rid) is None


def test_파일_DB는_다시_열어도_유지된다(tmp_path):
    path = tmp_path / "sub" / "r.db"
    Store(path).add_exclusion("U1")

    assert Store(path).exclusions() == {"U1"}


def test_리스트와_열_ID를_함께_기록하고_공유한_채널을_따로_남긴다(store):
    store.register_list("2026-10", "F1", {"date": "Col1", "user": "Col2"})

    assert store.get_list_columns("F1") == {"date": "Col1", "user": "Col2"}
    assert store.get_list_columns("F2") == {}
    assert store.list_shared_channel("2026-10") is None

    store.mark_list_shared("2026-10", "CADMIN")

    assert store.list_shared_channel("2026-10") == "CADMIN"


def test_지워진_달_리스트의_기록을_모두_지운다(store):
    store.register_list("2026-10", "F1", {"date": "Col1"})
    rid = store.add_report("U1", D1, "a", T1)
    store.set_list_item(rid, "F1", "I1")

    store.forget_list("2026-10")

    assert store.get_list_id("2026-10") is None
    assert store.get_list_columns("F1") == {}
    assert store.get_list_item(rid) is None


def test_리스트와_DB가_어긋난_보고를_찾는다(store):
    gone = store.add_report("U1", D1, "a", T1)
    store.set_list_item(gone, "F1", "I1")
    store.delete_report(gone)
    missing = store.add_report("U2", D2, "b", T1)
    old = store.add_report("U3", date(2026, 9, 1), "c", T1)

    assert store.orphan_list_items() == [gone]
    assert store.reports_without_list_item(since=date(2026, 10, 1)) == [missing]
    assert old not in store.reports_without_list_item(since=date(2026, 10, 1))


def test_작업_실행_날짜를_기간으로_모은다(store):
    store.mark_job_run("summary", D1, T1)
    store.mark_job_run("summary", D2, T1)
    store.mark_job_run("summary", date(2026, 9, 1), T1)

    assert store.job_days("summary", since=D1) == {D1, D2}


# 스키마 버전


def test_새_DB는_현재_스키마_버전으로_만든다(store):
    from daily_report.db import MIGRATIONS

    assert store.schema_version() == len(MIGRATIONS)


def test_새_스키마_변경은_기존_DB에_덧붙여_적용하고_적용_전에_백업한다(tmp_path):
    from daily_report.db import MIGRATIONS

    path = tmp_path / "r.db"
    old = Store(path)
    rid = old.add_report("U1", D1, "a", T1)
    old._db.close()

    newer = MIGRATIONS + ["ALTER TABLE reports ADD COLUMN extra TEXT;"]
    store = Store(path, migrations=newer)

    assert store.schema_version() == len(newer)
    assert store.get_report_by_id(rid).content == "a"  # 기존 데이터 유지
    assert (tmp_path / f"r.db.v{len(MIGRATIONS)}.bak").exists()


def test_코드보다_새_버전의_DB면_시작하지_않는다(tmp_path):
    from daily_report.db import MIGRATIONS, SchemaError

    path = tmp_path / "r.db"
    Store(path, migrations=MIGRATIONS + ["SELECT 1;"])._db.close()

    with pytest.raises(SchemaError):
        Store(path)


def test_기간의_보고를_모은다(store):
    store.add_report("U1", D1, "a", T1)
    store.add_report("U1", D2, "b", T1)
    store.add_report("U1", date(2026, 11, 2), "c", T1)

    found = store.reports_between(D1, D2)

    assert [r.content for r in found] == ["a", "b"]


def test_지운_보고의_ID는_다시_쓰지_않는다(store):
    rid = store.add_report("U1", D1, "a", T1)
    store.delete_report(rid)

    assert store.add_report("U1", D1, "b", T1) != rid


def test_사용자별_DM_발송을_기록한다(store):
    assert not store.dm_sent("request", D1, "U1")

    store.record_dm("request", D1, "U1", T1)
    store.record_dm("request", D1, "U1", T1)  # 두 번 기록해도 오류 없음

    assert store.dm_sent("request", D1, "U1")
    assert not store.dm_sent("request", D1, "U2")


def test_여러_스레드에서_동시에_써도_섞이지_않는다(store):
    import threading

    errors = []

    def work(i):
        try:
            for j in range(30):
                day = date(2026, 1, 1).toordinal() + j
                store.save_targets(date.fromordinal(day), {f"U{i}", "UX"})
                store.add_report(f"U{i}", date.fromordinal(day), "x", T1)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(store.reports_between(date(2026, 1, 1), date(2026, 12, 31))) == 240
