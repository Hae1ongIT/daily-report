"""날짜·텍스트 표시 형식."""

from datetime import date

WEEKDAYS = "월화수목금토일"


def md(day: date) -> str:
    return f"{day.month}/{day.day}"


def month_key(day: date) -> str:
    """월별 리스트를 구분하는 키(예: 2026-03). 리스트 이름과 기록에 같이 쓴다."""
    return f"{day.year:04d}-{day.month:02d}"


def month_day(day: date) -> str:
    """문장 속 날짜. '일'로 끝나서 뒤 조사는 늘 '은/이'다(예: 10월 20일은)."""
    return f"{day.month}월 {day.day}일"


def weekday(day: date) -> str:
    return WEEKDAYS[day.weekday()]


def day_label(day: date) -> str:
    return f"{md(day)}({weekday(day)})"


def escape(text: str) -> str:
    """Slack mrkdwn 이스케이프. 멘션·링크로 해석되지 않게 한다."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
