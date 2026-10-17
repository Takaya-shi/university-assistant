"""課題の締切を保存先アカウントの Google カレンダーへ登録する（Phase 6）。

- 書くのは保存先アカウント（storage）の、accounts.storage.deadline_calendar_id で指定した 1 つのカレンダーだけ。
  スコープは calendar.events.owned（本人が所有するカレンダーの予定だけ）。
- 予定の形は本人が手で入れていた締切に合わせる：締切の 30 分前〜締切の枠、通知は前日と 30 分前。
  時刻の無い締切（精度 date）は終日の予定にし、前日 9:00 に通知する。
- 状態ストアの課題一覧から「あるべき予定」を決め、前回書いた内容と違うときだけ API を呼ぶ。
  - 締切がある・未提出・締切前 → 予定を作る／更新する
  - 提出済み（TURNED_IN / RETURNED）→ 件名に【提出済】を付け、通知を外す（予定は消さない）
  - 締切が消えた・一覧から消えた → 予定を削除する
  - 締切を過ぎてから見つかった課題、提出済みで見つかった課題 → 新しく作らない
- 本人が予定を消した・件名や時刻を変えた場合は、その課題の予定にはもう触れない（要確認へ）。
- 予定には課題 ID を拡張プロパティで持たせ、状態ストアが失われても重複して作らない。
- dry-run では API を呼ばず、作成・更新・削除の予定件数だけ数える。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .collectors import CollectorError
from .timeutil import JST, iso_now, now_jst

SCOPES = ["https://www.googleapis.com/auth/calendar.events.owned"]
PROP = "usync_aid"
DONE_STATES = ("TURNED_IN", "RETURNED")
ACTIVE_REMINDERS = [{"method": "popup", "minutes": 1440}, {"method": "popup", "minutes": 30}]
ALLDAY_REMINDERS = [{"method": "popup", "minutes": 900}]  # 終日の予定は 0:00 起点。前日 9:00
SLOT = timedelta(minutes=30)


@dataclass
class CalendarReport:
    ok: bool = True
    detail: str = ""
    created: int = 0
    updated: int = 0
    removed: int = 0
    planned: dict = field(default_factory=dict)  # dry-run の予定件数 {"create": n, ...}
    reviews: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class GoogleCalendar:
    """保存先アカウントのカレンダー 1 つだけを扱う薄い包み。"""

    def __init__(self, cfg):
        from googleapiclient.discovery import build

        from .auth import load_credentials

        creds, self.account_id, self.email = load_credentials(cfg, "storage", SCOPES)
        self.events = build("calendar", "v3", credentials=creds, cache_discovery=False).events()
        self.calendar_id = cfg.calendar_id

    def _exec(self, request, what: str):
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=3)
        except HttpError as e:
            raise CollectorError(f"Calendar {what}: HTTP {e.resp.status} {e.reason}",
                                 permanent=e.resp.status in (400, 401, 403), status=e.resp.status)

    def get(self, event_id: str) -> dict | None:
        try:
            return self._exec(self.events.get(calendarId=self.calendar_id, eventId=event_id), f"取得 {event_id}")
        except CollectorError as e:
            if e.status in (404, 410):
                return None
            raise

    def find(self, assignment_id: str) -> dict | None:
        res = self._exec(self.events.list(calendarId=self.calendar_id, privateExtendedProperty=f"{PROP}={assignment_id}",
                                          showDeleted=False, maxResults=5), f"検索 {assignment_id}")
        items = res.get("items", [])
        return items[0] if items else None

    def insert(self, body: dict) -> dict:
        return self._exec(self.events.insert(calendarId=self.calendar_id, body=body), f"作成 {body['summary']}")

    def patch(self, event_id: str, body: dict) -> dict:
        return self._exec(self.events.patch(calendarId=self.calendar_id, eventId=event_id, body=body), f"更新 {body['summary']}")

    def delete(self, event_id: str) -> None:
        try:
            self._exec(self.events.delete(calendarId=self.calendar_id, eventId=event_id), f"削除 {event_id}")
        except CollectorError as e:
            if e.status not in (404, 410):
                raise


# ---------------------------------------------------------------- あるべき予定

def _due_label(due_at: str, precision: str) -> str:
    if precision == "datetime":
        d = datetime.fromisoformat(due_at)
        return f"締切時刻：{d.year}年{d.month}月{d.day}日 {d:%H:%M}"
    d = date.fromisoformat(due_at[:10])
    return f"締切日：{d.year}年{d.month}月{d.day}日（時刻の指定なし）"


def desired_event(row, course_name: str, now: datetime) -> tuple[dict, str] | None:
    """(予定の本文, "active" | "done") を返す。予定が要らなければ None。"""
    if not row["due_at"] or row["due_precision"] not in ("datetime", "date") or row["missing_since"]:
        return None
    done = row["submission_state"] in DONE_STATES
    snap = json.loads(row["snapshot"]) if row["snapshot"] else {}
    title = f"{course_name} {row['title']} 締切".strip()
    lines = [_due_label(row["due_at"], row["due_precision"])]
    if snap.get("url"):
        lines.append(f"課題：{snap['url']}")
    lines.append(f"Vault：{row['note_path']}")
    lines.append("university-sync が自動で登録・更新する予定。件名や時刻を手で変えると、以後この予定は自動更新しない。")
    body = {
        "summary": f"【提出済】{title}" if done else title,
        "description": "\n".join(lines),
        "transparency": "transparent",
        "extendedProperties": {"private": {PROP: row["assignment_id"]}},
    }
    if row["due_precision"] == "datetime":
        end = datetime.fromisoformat(row["due_at"]).astimezone(JST)
        body["start"] = {"dateTime": (end - SLOT).isoformat(timespec="seconds"), "timeZone": "Asia/Tokyo"}
        body["end"] = {"dateTime": end.isoformat(timespec="seconds"), "timeZone": "Asia/Tokyo"}
        reminders = ACTIVE_REMINDERS
    else:
        d = date.fromisoformat(row["due_at"][:10])
        body["start"] = {"date": d.isoformat()}
        body["end"] = {"date": (d + timedelta(days=1)).isoformat()}
        reminders = ALLDAY_REMINDERS
    body["reminders"] = {"useDefault": False, "overrides": [] if done else reminders}
    return body, "done" if done else "active"


def _norm(t: dict) -> str:
    if "date" in t:
        return t["date"]
    return datetime.fromisoformat(t["dateTime"]).astimezone(JST).isoformat()


def fingerprint(ev: dict) -> str:
    """本人の編集を見分けるための件名・開始・終了。書式の違い（秒・オフセット）は吸収する。"""
    return json.dumps([ev.get("summary", ""), _norm(ev["start"]), _norm(ev["end"])], ensure_ascii=False)


def _due_passed(row, now: datetime) -> bool:
    if row["due_precision"] == "datetime":
        return datetime.fromisoformat(row["due_at"]) <= now
    return date.fromisoformat(row["due_at"][:10]) < now.date()


# ---------------------------------------------------------------- 同期

def sync_calendar(cfg, store, cal, *, dry_run: bool, now: datetime | None = None) -> CalendarReport:
    now = now or now_jst()
    report = CalendarReport()
    plan = {"create": 0, "update": 0, "remove": 0}
    for row in store.all_assignments():
        aid = row["assignment_id"]
        course = cfg.course_by_id(row["course_id"])
        want = desired_event(row, course.name if course else "", now)
        rec = store.calendar_event(aid)
        if rec is not None and rec["calendar_id"] != cfg.calendar_id:
            rec = None  # 登録先のカレンダーを変えた。前のカレンダーの予定には触れない
        if rec is not None and rec["status"] in ("user_deleted", "user_edited"):
            continue
        live = rec is not None and rec["status"] in ("active", "done") and rec["event_id"]
        try:
            if want is None:
                if live:
                    plan["remove"] += 1
                    if not dry_run:
                        cal.delete(rec["event_id"])
                        store.set_calendar_event(aid, cfg.calendar_id, None, None, "removed", iso_now())
                        report.removed += 1
                continue
            body, mode = want
            if not live:
                if mode == "done" or _due_passed(row, now):
                    continue
                plan["create"] += 1
                if dry_run:
                    continue
                existing = cal.find(aid)  # 状態ストアを失った後の再実行でも重複させない
                ev = cal.patch(existing["id"], body) if existing else cal.insert(body)
                store.set_calendar_event(aid, cfg.calendar_id, ev["id"], fingerprint(body), mode, iso_now())
                report.created += 1
                continue
            if fingerprint(body) == rec["fingerprint"] and mode == rec["status"]:
                continue
            plan["update"] += 1
            if dry_run:
                continue
            ev = cal.get(rec["event_id"])
            if ev is None or ev.get("status") == "cancelled":
                store.set_calendar_event(aid, cfg.calendar_id, rec["event_id"], rec["fingerprint"], "user_deleted", iso_now())
                report.reviews.append((f"calendar_deleted:{aid}", "カレンダーの予定を本人が削除", aid,
                                       f"{row['title']}: 締切の予定が削除されていたので作り直さない（以後この課題の予定は自動で扱わない）"))
                continue
            if fingerprint(ev) != rec["fingerprint"]:
                store.set_calendar_event(aid, cfg.calendar_id, rec["event_id"], rec["fingerprint"], "user_edited", iso_now())
                report.reviews.append((f"calendar_edited:{aid}", "カレンダーの予定を本人が編集", aid,
                                       f"{row['title']}: 締切の予定の件名か時刻が手で変えられていたので上書きしない"
                                       f"（学校側の締切 {row['due_at']}）"))
                continue
            cal.patch(rec["event_id"], body)
            store.set_calendar_event(aid, cfg.calendar_id, rec["event_id"], fingerprint(body), mode, iso_now())
            report.updated += 1
        except CollectorError as e:
            report.errors.append(f"{row['title']}: {e}")
    report.planned = plan
    if report.errors:
        report.ok = False
        report.detail = f"{len(report.errors)} 件を反映できなかった（次回再試行）"
    return report
