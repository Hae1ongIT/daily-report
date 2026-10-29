# daily-report

Slack에서 팀원의 일일보고를 간단히 받고, 관리자에게 사내 LLM 요약을 주는 봇.

## 구조

```
daily-report/
├── README.md          # 이 파일
├── pyproject.toml     # Python 프로젝트, 의존성 (uv)
├── .env.example       # 설정 키 목록 (값은 .env에)
├── manifest.yml       # Slack 앱 생성용 매니페스트
├── src/daily_report/
│   ├── app.py         # 진입점: Bolt(Socket Mode), 작업별 매분 예약, 시작 점검
│   ├── handlers.py    # 버튼·모달·이벤트 처리
│   ├── jobs.py        # 17:30 요청, 09:00 재알림, 10:00 요약, 그 달 리스트, 늦은 실행·놓친 요약 알림
│   ├── home.py        # 앱 홈 날짜 목록(상태 판정)
│   ├── views.py       # Block Kit 화면
│   ├── reports.py     # 보고 규칙, 기본 날짜
│   ├── leaves.py      # 연차·반차
│   ├── targets.py     # 대상자, 휴무일·출근일 지정
│   ├── summary.py     # 요약 집계(사람은 사용자 ID로 구분)
│   ├── llm.py         # 사내 vLLM 요약, 재시도
│   ├── slack_lists.py # 월별 관리자 리스트
│   ├── workcal.py     # 근무일 달력
│   ├── db.py          # SQLite(모든 접근을 락으로 직렬화)
│   └── config.py, clock.py, directory.py, members.py, errors.py, fmt.py
└── tests/             # pytest
```

## 설치와 실행

1. Slack 앱 만들기: api.slack.com/apps → Create New App → From a manifest → `manifest.yml` 붙여넣기 → 워크스페이스에 설치(관리자 권한).
2. 토큰 발급
   - 봇 토큰(`xoxb-`): OAuth & Permissions
   - 앱 토큰(`xapp-`): Basic Information → App-Level Tokens → `connections:write` 범위로 생성
3. 관리자 비공개 채널을 만들고 봇(`/invite @daily-report`)과 관리자를 초대한다. 채널 ID를 확인한다.
   - 봇은 시작할 때 이 채널이 비공개인지, 봇이 멤버인지 확인하고, 아니면 시작하지 않는다(요약과 원본 리스트가 이 채널에 공유되기 때문).
4. `.env.example`을 `.env`로 복사하고 값을 채운다. 키마다 어디서 가져오는지 `.env.example`의 주석에 적혀 있다.
   - 필수 5개: 앱 토큰, 봇 토큰, 관리자 채널 ID, `LLM_BASE_URL`(`/v1`까지), `LLM_MODEL`
   - 처음에는 `TEST_TARGET_USERS`에 시험할 사람(본인 등)만 넣는다. 사용자 ID는 Slack 프로필 → ⋮ → "멤버 ID 복사"
   - DB는 기본으로 이 폴더의 `data/daily_report.db`에 생긴다(실행 위치와 상관없음).
   - 처음 시험 예시(평일 14:00에 시험한다면). 값은 본인 것으로 바꾼다.

```
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
ADMIN_CHANNEL_ID=C0123456789
LLM_BASE_URL=http://사내주소:8000/v1
LLM_MODEL=모델이름
TEST_TARGET_USERS=U0123456789
REQUEST_TIME=14:05
REMINDER_TIME=14:10
SUMMARY_TIME=14:15
SUMMARY_DEADLINE=18:00
```

   - 위처럼 두면 14:05 요청 DM이 온다. 재알림·요약은 "직전 근무일" 보고를 다루므로 봇을 처음 켠 날에는 나오지 않는다(설치 전 날짜는 다루지 않음). 다음 근무일에 확인하거나, 요청 DM → 보고 작성 → 앱 홈 → 관리자 리스트 행 생성까지를 첫날 확인한다.
   - 시각이 설정 순서(요청 < 시한 등)에 맞지 않으면 봇이 시작하지 않고 어떤 값이 문제인지 알려 준다.
5. 실행

```
uv sync
uv run python -m daily_report.app
```

- 매니페스트의 권한(scope)을 바꾸면 앱을 다시 설치해야 한다.
- 로그는 표준 출력으로 나간다. 시작 시 설정 요약, 작업마다 보낸 DM 수·요약 게시 결과가 남는다.
- Slack 라이브러리 로그를 DEBUG로 올리지 않는다. 요청 본문(보고 내용)이 로그에 찍힐 수 있다.

## 운영

- **봇은 하나만 띄운다.** 같은 DB로 두 번째를 띄우면 시작하지 않는다(`data/daily_report.lock`). 다른 PC에서 같은 토큰으로 띄우는 것은 막을 수 없으니, 개발·시험용 Slack 앱을 따로 만들어 쓴다(같은 앱 토큰이면 버튼 이벤트가 두 프로세스로 나뉘고 DM·요약이 중복된다).
- **서버 설치**: 서버의 Python 버전(3.12 이상)과 인터넷 접근 여부를 먼저 확인한다.
  - 인터넷이 되면: `uv sync --locked --no-dev`
  - 막혀 있으면: 개발 PC에서 `uv export --no-dev --frozen > requirements.txt`로 목록을 만들고 휠을 미리 받아 `pip install --no-index --find-links`로 설치한다.
  - 실행은 `.venv/bin/python -m daily_report.app` (`uv run`은 실행할 때마다 동기화를 시도한다).
- **systemd 예시** (`/etc/systemd/system/daily-report.service`)

```ini
[Unit]
Description=daily-report Slack bot
After=network-online.target
Wants=network-online.target

[Service]
User=dailyreport
WorkingDirectory=/home/daily-report
ExecStart=/home/daily-report/.venv/bin/python -m daily_report.app
Restart=on-failure
RestartSec=10
# 설정 오류(종료 코드 78)는 다시 시작해도 소용없다
RestartPreventExitStatus=78
Environment=PYTHONUNBUFFERED=1
UMask=0077
# 끌 때 진행 중인 LLM 요청 하나를 기다린다(최대 LLM_TIMEOUT_SECONDS, 기본 120초)
TimeoutStopSec=150

[Install]
WantedBy=multi-user.target
```

- **파일 권한**: `.env`(토큰)는 `chmod 600`. 봇은 시작할 때 umask를 077로 두어 DB와 백업을 소유자만 읽게 만든다.
- **지우면 안 되는 것**: `data/`(DB, 백업)와 `.env`는 커밋 대상이 아니라서 `git clean -fdx`로 지워진다. 배포할 때 쓰지 않는다.
- **소스 폴더 밖에 설치**하면 `DAILY_REPORT_HOME`에 `.env`와 `data/`가 있는 폴더를 지정한다.
- **DB 스키마**: 시작할 때 `PRAGMA user_version`을 보고 새 변경만 적용한다. 적용 전 `data/daily_report.db.v<버전>.bak`으로 백업한다. 코드보다 새 DB면 시작하지 않는다.
- **공휴일 데이터**: `holidays` 패키지는 다음 해 공휴일·임시공휴일을 연말쯤 반영한다. 해마다 12월에 의존성을 올리고, 늦게 발표된 임시공휴일은 관리 메뉴의 휴무일로 추가한다.
- **시간대(`TIMEZONE`)는 운영 중에 바꾸지 않는다.** 날짜별 기록의 경계가 어긋난다.
- **대상자 관련 변경(시험 모드 해제, 제외 명단)은 17:30 전에 한다.** 그날 대상자 목록은 17:30에 정해지고 그 뒤에는 고칠 수 없다.
- **관리자 리스트**: 하루 한 번 DB와 어긋난 행(빠진 행, 수정·이동이 반영 안 된 행, 지운 보고의 행)을 자동으로 맞추고, 맞추지 못한 건이 있으면 관리자 채널에 알린다. 그때는 관리 메뉴의 리스트 동기화를 쓴다. 팀이 45명을 넘으면 한 달 리스트가 1,000행 한도에 가까워진다.

## 개발

```
uv run pytest
uv run ruff check
uv run ruff format --check
```

## 상태

정식 운영 중.
