"""SQLite 저장소. 봇 데이터의 원본이다(SPEC: DB 원본, 리스트는 한 방향 기록)."""

import functools
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ANNUAL = "annual"
HALF = "half"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    report_date TEXT NOT NULL,
    content TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE (user_id, report_date)
);
CREATE TABLE IF NOT EXISTS leaves (
    user_id TEXT NOT NULL,
    leave_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('annual', 'half')),
    PRIMARY KEY (user_id, leave_date)
);
CREATE TABLE IF NOT EXISTS exclusions (user_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS calendar_overrides (
    day TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('off', 'work'))
);
CREATE TABLE IF NOT EXISTS target_days (day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS target_users (
    day TEXT NOT NULL REFERENCES target_days (day) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    PRIMARY KEY (day, user_id)
);
CREATE TABLE IF NOT EXISTS job_runs (
    job TEXT NOT NULL,
    target_day TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    PRIMARY KEY (job, target_day)
);
CREATE TABLE IF NOT EXISTS list_registry (
    month TEXT PRIMARY KEY,
    list_id TEXT NOT NULL,
    shared_channel TEXT
);
CREATE TABLE IF NOT EXISTS dm_log (
    job TEXT NOT NULL,
    target_day TEXT NOT NULL,
    user_id TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (job, target_day, user_id)
);
CREATE TABLE IF NOT EXISTS list_columns (
    list_id TEXT NOT NULL,
    key TEXT NOT NULL,
    column_id TEXT NOT NULL,
    PRIMARY KEY (list_id, key)
);
CREATE TABLE IF NOT EXISTS list_items (
    report_id INTEGER PRIMARY KEY,
    list_id TEXT NOT NULL,
    item_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reports_date ON reports (report_date);
"""

# 스키마 변경은 이 목록 끝에 SQL을 덧붙인다(이미 적용된 항목은 고치지 않는다).
# DB의 PRAGMA user_version이 적용된 개수다. 1번은 처음 배포한 스키마 전체다.
MIGRATIONS: list[str] = [
    SCHEMA,
    # 2: 달을 옮길 때 지우지 못한 옛 리스트 행(정합성 점검이 다시 지운다)
    """CREATE TABLE IF NOT EXISTS stale_list_items (
        list_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        PRIMARY KEY (list_id, item_id)
    );""",
    # 3: 리스트 행에 마지막으로 기록한 보고 상태(날짜|수정 시각). 수정·이동 기록이 실패해
    # 어긋난 행을 정합성 점검이 찾는다.
    "ALTER TABLE list_items ADD COLUMN synced TEXT;",
]


class SchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Report:
    id: int
    user_id: str
    report_date: date
    content: str
    submitted_at: datetime
    updated_at: datetime | None


class Store:
    """모든 메서드는 하나의 락 안에서 돈다. Bolt 핸들러 스레드와 예약 작업 스레드가
    연결 하나를 같이 쓰므로, 트랜잭션이 섞이지 않게 한다. 여러 호출을 한 덩어리로
    처리해야 하면 `with store.lock:`으로 감싼다(RLock이라 안에서 다시 잡아도 된다)."""

    def __init__(self, path: str | Path, migrations: list[str] | None = None):
        self.lock = threading.RLock()
        self._path = str(path)
        if self._path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._migrate(MIGRATIONS if migrations is None else migrations)

    def _migrate(self, migrations: list[str]) -> None:
        """user_version 이후의 변경만 차례로 적용한다. 적용 전에 파일 DB는 백업한다."""
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(migrations):
            raise SchemaError(f"DB 버전({version})이 코드({len(migrations)})보다 새로워요.")
        if version == len(migrations):
            return
        if version > 0 and self._path != ":memory:":
            backup = sqlite3.connect(f"{self._path}.v{version}.bak")
            with backup:
                self._db.backup(backup)
            backup.close()
        for i in range(version, len(migrations)):
            self._db.executescript(
                f"BEGIN; {migrations[i]}\nPRAGMA user_version = {i + 1}; COMMIT;"
            )

    def schema_version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]

    # 보고

    def add_report(self, user_id: str, day: date, content: str, now: datetime) -> int:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO reports (user_id, report_date, content, submitted_at)"
                " VALUES (?, ?, ?, ?)",
                (user_id, day.isoformat(), content, now.isoformat()),
            )
        return cur.lastrowid

    def update_report(self, report_id: int, content: str, now: datetime) -> None:
        with self._db:
            self._db.execute(
                "UPDATE reports SET content = ?, updated_at = ? WHERE id = ?",
                (content, now.isoformat(), report_id),
            )

    def move_report(self, report_id: int, day: date, content: str, now: datetime) -> None:
        with self._db:
            self._db.execute(
                "UPDATE reports SET report_date = ?, content = ?, updated_at = ? WHERE id = ?",
                (day.isoformat(), content, now.isoformat(), report_id),
            )

    def delete_report(self, report_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM reports WHERE id = ?", (report_id,))

    def get_report(self, user_id: str, day: date) -> Report | None:
        row = self._db.execute(
            "SELECT * FROM reports WHERE user_id = ? AND report_date = ?",
            (user_id, day.isoformat()),
        ).fetchone()
        return _report(row) if row else None

    def get_report_by_id(self, report_id: int) -> Report | None:
        row = self._db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return _report(row) if row else None

    def reports_on(self, days: Iterable[date]) -> list[Report]:
        keys = [d.isoformat() for d in days]
        if not keys:
            return []
        rows = self._db.execute(
            f"SELECT * FROM reports WHERE report_date IN ({','.join('?' * len(keys))})"
            " ORDER BY report_date, user_id",
            keys,
        ).fetchall()
        return [_report(r) for r in rows]

    def reports_between(self, start: date, end: date) -> list[Report]:
        rows = self._db.execute(
            "SELECT * FROM reports WHERE report_date BETWEEN ? AND ? ORDER BY report_date, user_id",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [_report(r) for r in rows]

    # 연차·반차

    def add_leaves(self, user_id: str, days: Iterable[date], kind: str) -> None:
        with self._db:
            self._db.executemany(
                "INSERT INTO leaves (user_id, leave_date, kind) VALUES (?, ?, ?)",
                [(user_id, d.isoformat(), kind) for d in days],
            )

    def delete_leaves(self, user_id: str, days: Iterable[date]) -> None:
        with self._db:
            self._db.executemany(
                "DELETE FROM leaves WHERE user_id = ? AND leave_date = ?",
                [(user_id, d.isoformat()) for d in days],
            )

    def get_leave(self, user_id: str, day: date) -> str | None:
        row = self._db.execute(
            "SELECT kind FROM leaves WHERE user_id = ? AND leave_date = ?",
            (user_id, day.isoformat()),
        ).fetchone()
        return row["kind"] if row else None

    def leaves_between(self, start: date, end: date) -> list[tuple[str, date, str]]:
        """start~end(포함)의 모든 사람 연차·반차 (사용자, 날짜, 종류). 사람, 날짜순."""
        rows = self._db.execute(
            "SELECT user_id, leave_date, kind FROM leaves WHERE leave_date BETWEEN ? AND ?"
            " ORDER BY user_id, leave_date",
            (start.isoformat(), end.isoformat()),
        )
        return [(r["user_id"], date.fromisoformat(r["leave_date"]), r["kind"]) for r in rows]

    def leaves_after(self, user_id: str, day: date) -> list[tuple[date, str]]:
        """day 다음 날부터의 연차·반차(날짜순)."""
        rows = self._db.execute(
            "SELECT leave_date, kind FROM leaves WHERE user_id = ? AND leave_date > ?"
            " ORDER BY leave_date",
            (user_id, day.isoformat()),
        )
        return [(date.fromisoformat(r["leave_date"]), r["kind"]) for r in rows]

    def leaves_on(self, day: date) -> dict[str, str]:
        rows = self._db.execute(
            "SELECT user_id, kind FROM leaves WHERE leave_date = ?", (day.isoformat(),)
        ).fetchall()
        return {r["user_id"]: r["kind"] for r in rows}

    # 제외 명단

    def add_exclusion(self, user_id: str) -> None:
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO exclusions VALUES (?)", (user_id,))

    def remove_exclusion(self, user_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM exclusions WHERE user_id = ?", (user_id,))

    def exclusions(self) -> set[str]:
        return {r["user_id"] for r in self._db.execute("SELECT user_id FROM exclusions")}

    # 휴무일·출근일 지정

    def set_override(self, day: date, kind: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO calendar_overrides (day, kind) VALUES (?, ?)"
                " ON CONFLICT (day) DO UPDATE SET kind = excluded.kind",
                (day.isoformat(), kind),
            )

    def remove_override(self, day: date) -> None:
        with self._db:
            self._db.execute("DELETE FROM calendar_overrides WHERE day = ?", (day.isoformat(),))

    def overrides(self) -> dict[date, str]:
        rows = self._db.execute("SELECT day, kind FROM calendar_overrides")
        return {date.fromisoformat(r["day"]): r["kind"] for r in rows}

    # 날짜별 대상자 목록

    def save_targets(self, day: date, user_ids: Iterable[str]) -> None:
        key = day.isoformat()
        with self._db:
            self._db.execute("DELETE FROM target_days WHERE day = ?", (key,))
            self._db.execute("INSERT INTO target_days VALUES (?)", (key,))
            self._db.executemany(
                "INSERT INTO target_users VALUES (?, ?)", [(key, u) for u in user_ids]
            )

    def get_targets(self, day: date) -> set[str] | None:
        key = day.isoformat()
        if not self._db.execute("SELECT 1 FROM target_days WHERE day = ?", (key,)).fetchone():
            return None
        rows = self._db.execute("SELECT user_id FROM target_users WHERE day = ?", (key,))
        return {r["user_id"] for r in rows}

    def delete_targets(self, day: date) -> None:
        with self._db:
            self._db.execute("DELETE FROM target_days WHERE day = ?", (day.isoformat(),))

    # 예약 작업 실행 기록

    def mark_job_run(self, job: str, day: date, now: datetime) -> bool:
        """처음 기록하면 True, 이미 실행된 작업이면 False."""
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO job_runs VALUES (?, ?, ?)",
                (job, day.isoformat(), now.isoformat()),
            )
        return cur.rowcount == 1

    def has_job_run(self, job: str, day: date) -> bool:
        return bool(
            self._db.execute(
                "SELECT 1 FROM job_runs WHERE job = ? AND target_day = ?", (job, day.isoformat())
            ).fetchone()
        )

    def job_days(self, job: str, since: date) -> set[date]:
        rows = self._db.execute(
            "SELECT target_day FROM job_runs WHERE job = ? AND target_day >= ?",
            (job, since.isoformat()),
        )
        return {date.fromisoformat(r["target_day"]) for r in rows}

    def first_job_day(self, job: str) -> date | None:
        row = self._db.execute(
            "SELECT MIN(target_day) AS d FROM job_runs WHERE job = ?", (job,)
        ).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    # 사용자별 DM 발송 기록(작업이 중간에 끊겨도 받은 사람에게 다시 보내지 않는다)

    def dm_sent(self, job: str, day: date, user_id: str) -> bool:
        return bool(
            self._db.execute(
                "SELECT 1 FROM dm_log WHERE job = ? AND target_day = ? AND user_id = ?",
                (job, day.isoformat(), user_id),
            ).fetchone()
        )

    def record_dm(self, job: str, day: date, user_id: str, now: datetime) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO dm_log VALUES (?, ?, ?, ?)",
                (job, day.isoformat(), user_id, now.isoformat()),
            )

    # 월별 관리자 리스트

    def get_list_id(self, month: str) -> str | None:
        row = self._db.execute(
            "SELECT list_id FROM list_registry WHERE month = ?", (month,)
        ).fetchone()
        return row["list_id"] if row else None

    def register_list(self, month: str, list_id: str, columns: dict[str, str]) -> None:
        """만든 리스트를 바로 기록한다(공유 전). 공유는 mark_list_shared로 따로 남긴다."""
        with self._db:
            self._db.execute(
                "INSERT INTO list_registry (month, list_id) VALUES (?, ?)", (month, list_id)
            )
            self._db.executemany(
                "INSERT OR REPLACE INTO list_columns VALUES (?, ?, ?)",
                [(list_id, k, c) for k, c in columns.items()],
            )

    def mark_list_shared(self, month: str, channel_id: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE list_registry SET shared_channel = ? WHERE month = ?", (channel_id, month)
            )

    def list_shared_channel(self, month: str) -> str | None:
        row = self._db.execute(
            "SELECT shared_channel FROM list_registry WHERE month = ?", (month,)
        ).fetchone()
        return row["shared_channel"] if row else None

    def forget_list(self, month: str) -> None:
        """Slack에서 지워진 달 리스트의 기록을 지운다(다음에 새로 만든다)."""
        list_id = self.get_list_id(month)
        if not list_id:
            return
        with self._db:
            self._db.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
            self._db.execute("DELETE FROM list_columns WHERE list_id = ?", (list_id,))
            self._db.execute("DELETE FROM list_registry WHERE month = ?", (month,))

    def orphan_list_items(self) -> list[int]:
        """보고는 지워졌는데 리스트 행 기록이 남은 보고 ID."""
        rows = self._db.execute(
            "SELECT li.report_id FROM list_items li"
            " LEFT JOIN reports r ON r.id = li.report_id WHERE r.id IS NULL"
        )
        return [r["report_id"] for r in rows]

    def reports_without_list_item(self, since: date) -> list[int]:
        """since 이후 날짜의 보고 중 리스트 행 기록이 없는 보고 ID."""
        rows = self._db.execute(
            "SELECT r.id FROM reports r LEFT JOIN list_items li ON li.report_id = r.id"
            " WHERE li.report_id IS NULL AND r.report_date >= ? ORDER BY r.id",
            (since.isoformat(),),
        )
        return [r["id"] for r in rows]

    def get_list_columns(self, list_id: str) -> dict[str, str]:
        rows = self._db.execute(
            "SELECT key, column_id FROM list_columns WHERE list_id = ?", (list_id,)
        )
        return {r["key"]: r["column_id"] for r in rows}

    def get_list_item(self, report_id: int) -> tuple[str, str] | None:
        row = self._db.execute(
            "SELECT list_id, item_id FROM list_items WHERE report_id = ?", (report_id,)
        ).fetchone()
        return (row["list_id"], row["item_id"]) if row else None

    def set_list_item(
        self, report_id: int, list_id: str, item_id: str, synced: str | None = None
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO list_items (report_id, list_id, item_id, synced) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (report_id) DO UPDATE SET list_id = excluded.list_id,"
                " item_id = excluded.item_id, synced = excluded.synced",
                (report_id, list_id, item_id, synced),
            )

    def mark_list_item_synced(self, report_id: int, synced: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE list_items SET synced = ? WHERE report_id = ?", (synced, report_id)
            )

    def reports_with_stale_list_item(self, since: date) -> list[int]:
        """since 이후 보고 중 리스트 행에 마지막으로 기록한 상태가 지금 보고와 다른 보고 ID.
        상태 문자열은 slack_lists.synced_state와 같은 형식이다."""
        rows = self._db.execute(
            "SELECT r.id FROM reports r JOIN list_items li ON li.report_id = r.id"
            " WHERE r.report_date >= ? AND li.synced IS NOT"
            " r.report_date || '|' || COALESCE(r.updated_at, r.submitted_at) ORDER BY r.id",
            (since.isoformat(),),
        )
        return [r["id"] for r in rows]

    def delete_list_item(self, report_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM list_items WHERE report_id = ?", (report_id,))

    def add_stale_list_item(self, list_id: str, item_id: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO stale_list_items VALUES (?, ?)", (list_id, item_id)
            )

    def stale_list_items(self) -> list[tuple[str, str]]:
        rows = self._db.execute("SELECT list_id, item_id FROM stale_list_items ORDER BY rowid")
        return [(r["list_id"], r["item_id"]) for r in rows]

    def remove_stale_list_item(self, list_id: str, item_id: str) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM stale_list_items WHERE list_id = ? AND item_id = ?",
                (list_id, item_id),
            )


def _locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


for _name, _method in list(vars(Store).items()):
    if callable(_method) and not _name.startswith("_"):
        setattr(Store, _name, _locked(_method))


def _report(row: sqlite3.Row) -> Report:
    return Report(
        id=row["id"],
        user_id=row["user_id"],
        report_date=date.fromisoformat(row["report_date"]),
        content=row["content"],
        submitted_at=datetime.fromisoformat(row["submitted_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )
