"""
Arbitrage-X — ISO 주차 유틸리티
week_key 형식: "2026-W18"
"""
from __future__ import annotations

from datetime import date, timedelta


def get_current_week_key() -> str:
    """현재 날짜의 ISO 주차 키를 반환한다. 예: '2026-W18'"""
    today = date.today()
    iso = today.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def get_week_bounds(week_key: str) -> tuple[date, date]:
    """
    week_key로부터 해당 주 월요일(시작)과 일요일(종료)을 반환한다.
    예: "2026-W18" → (2026-04-27, 2026-05-03)
    """
    year, week = parse_week_key(week_key)
    monday = date.fromisocalendar(year, week, 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def parse_week_key(week_key: str) -> tuple[int, int]:
    """
    "2026-W18" → (2026, 18)
    Raises ValueError if format is invalid.
    """
    try:
        parts = week_key.split("-W")
        if len(parts) != 2:
            raise ValueError
        year, week = int(parts[0]), int(parts[1])
        if not (1 <= week <= 53):
            raise ValueError
        return year, week
    except (ValueError, AttributeError):
        raise ValueError(
            f"Invalid week_key format: '{week_key}'. Expected 'YYYY-WNN'."
        )


def week_key_from_date(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def is_monday() -> bool:
    return date.today().weekday() == 0


def weeks_between(from_key: str, to_key: str) -> list[str]:
    """from_key ~ to_key 사이 모든 주차 키 목록 반환 (양 끝 포함)."""
    fy, fw = parse_week_key(from_key)
    ty, tw = parse_week_key(to_key)
    start = date.fromisocalendar(fy, fw, 1)
    end = date.fromisocalendar(ty, tw, 1)

    keys = []
    current = start
    while current <= end:
        keys.append(week_key_from_date(current))
        current += timedelta(weeks=1)
    return keys
