"""日時の正規化。表示基準は Asia/Tokyo（設計書 8.3）。"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def now_jst() -> datetime:
    return datetime.now(JST)


def iso_now() -> str:
    return now_jst().isoformat(timespec="seconds")


def classroom_due_to_jst(due_date: dict | None, due_time: dict | None) -> tuple[str | None, str]:
    """Classroom の dueDate / dueTime（UTC）を JST の ISO 文字列と精度に変換する。

    日付だけを JST の日付として解釈しない。必ず UTC の日時を組み立ててから変換する。
    TimeOfDay は 0 のフィールドが省略されるため、欠けた値は 0 とみなす。
    dueTime が無い場合は時刻を補わず、精度 date として UTC 日付をそのまま返す。
    """
    if not due_date:
        return None, "unknown"
    y, m, d = due_date.get("year"), due_date.get("month"), due_date.get("day")
    if not (y and m and d):
        return None, "unknown"
    if due_time is None:
        return f"{y:04d}-{m:02d}-{d:02d}", "date"
    utc = datetime(
        y, m, d,
        due_time.get("hours", 0),
        due_time.get("minutes", 0),
        due_time.get("seconds", 0),
        tzinfo=timezone.utc,
    )
    return utc.astimezone(JST).isoformat(timespec="minutes"), "datetime"
