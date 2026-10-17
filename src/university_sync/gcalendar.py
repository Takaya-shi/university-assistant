"""締切を保存先アカウントの Google カレンダーへ登録する（Phase 6）。

- 書くのは保存先アカウント（storage）の、accounts.storage.deadline_calendar_id で指定した 1 つのカレンダーだけ。
  スコープは calendar.events.owned（本人が所有するカレンダーの予定だけ）。
- 予定の形：締切の 1 時間前〜締切の枠、通知は開始の前日と 30 分前（本人指定 2026-10-18）。
  時刻の無い締切は終日の予定にし、前日 9:00 に通知する。
- 対象は 2 種類。
  1. 同期で管理している課題（状態ストア）。学校側の締切を使う。
  2. 大学タスクのノートに本人が手で書いた、📅 付きのタスク行（Moodle・WebClass の課題、小テストなど）。
     学校側に締切が無い管理課題の行に本人が 📅 を付けた場合もこちらで扱う。
     時刻は行の文面から読む（「10/26 00:00まで」「〜23:59」「19:30まで」）。読めなければ終日。
- 「あるべき予定」と前回書いた内容が違うときだけ API を呼ぶ。
  - 未完了・締切前 → 作る／更新する
  - 完了（学校側の提出確認、または行のチェック）→ 件名に【提出済】【完了】を付け、通知を外す（予定は消さない）
  - 締切が消えた・課題や行が消えた・行を取り消し（[-]）にした → 予定を削除する
  - 締切を過ぎてから見つかったもの、完了済みで見つかったもの → 新しく作らない
- 本人が予定を消した・件名や時刻を変えた場合は、その予定にはもう触れない（要確認へ）。
- 手書きの行は、同じ日に本人が手で入れた同名の予定（例：試験）が既にあれば作らない。
- 予定には課題 ID（手書きの行は行の指紋）を拡張プロパティで持たせ、状態ストアを失っても重複して作らない。
- dry-run では API を呼ばず、作成・更新・削除の予定件数だけ数える。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .collectors import CollectorError
from .timeutil import JST, iso_now, now_jst

SCOPES = ["https://www.googleapis.com/auth/calendar.events.owned"]
PROP = "usync_aid"      # 管理課題の予定に付ける拡張プロパティ
TASK_PROP = "usync_task"  # 手書きタスク行の予定に付ける拡張プロパティ
DONE_STATES = ("TURNED_IN", "RETURNED")
ACTIVE_REMINDERS = [{"method": "popup", "minutes": 1440}, {"method": "popup", "minutes": 30}]
ALLDAY_REMINDERS = [{"method": "popup", "minutes": 900}]  # 終日の予定は 0:00 起点。前日 9:00
SLOT = timedelta(hours=1)
NOTE_AUTO = "university-sync が自動で登録・更新する予定。件名や時刻を手で変えると、以後この予定は自動更新しない。"


@dataclass
class CalendarReport:
    ok: bool = True
    detail: str = ""
    created: int = 0
    updated: int = 0
    removed: int = 0
    planned: dict = field(default_factory=lambda: {"create": 0, "update": 0, "remove": 0})  # dry-run の予定件数
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

    def find(self, prop: str, value: str) -> dict | None:
        res = self._exec(self.events.list(calendarId=self.calendar_id, privateExtendedProperty=f"{prop}={value}",
                                          showDeleted=False, maxResults=5), f"検索 {value}")
        items = res.get("items", [])
        return items[0] if items else None

    def on_day(self, day: date) -> list[dict]:
        start = datetime(day.year, day.month, day.day, tzinfo=JST)
        res = self._exec(self.events.list(calendarId=self.calendar_id, timeMin=start.isoformat(),
                                          timeMax=(start + timedelta(days=1)).isoformat(), singleEvents=True,
                                          showDeleted=False, maxResults=50), f"一覧 {day}")
        return res.get("items", [])

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


# ---------------------------------------------------------------- 予定の形

def _due_label(due: datetime | date) -> str:
    if isinstance(due, datetime):
        return f"締切時刻：{due.year}年{due.month}月{due.day}日 {due:%H:%M}"
    return f"締切日：{due.year}年{due.month}月{due.day}日（時刻の指定なし）"


def _event_body(summary: str, due: datetime | date, lines: list[str], prop: str, key: str, done: bool) -> dict:
    body = {
        "summary": summary,
        "description": "\n".join([_due_label(due), *lines, NOTE_AUTO]),
        "transparency": "transparent",
        "extendedProperties": {"private": {prop: key}},
    }
    if isinstance(due, datetime):
        end = due.astimezone(JST)
        body["start"] = {"dateTime": (end - SLOT).isoformat(timespec="seconds"), "timeZone": "Asia/Tokyo"}
        body["end"] = {"dateTime": end.isoformat(timespec="seconds"), "timeZone": "Asia/Tokyo"}
        reminders = ACTIVE_REMINDERS
    else:
        body["start"] = {"date": due.isoformat()}
        body["end"] = {"date": (due + timedelta(days=1)).isoformat()}
        reminders = ALLDAY_REMINDERS
    body["reminders"] = {"useDefault": False, "overrides": [] if done else reminders}
    return body


def _row_due(row) -> datetime | date | None:
    if not row["due_at"] or row["missing_since"]:
        return None
    if row["due_precision"] == "datetime":
        return datetime.fromisoformat(row["due_at"])
    if row["due_precision"] == "date":
        return date.fromisoformat(row["due_at"][:10])
    return None


def desired_event(row, course_name: str, now: datetime | None = None) -> tuple[dict, str] | None:
    """管理課題の (予定の本文, "active" | "done")。予定が要らなければ None。"""
    due = _row_due(row)
    if due is None:
        return None
    done = row["submission_state"] in DONE_STATES
    snap = json.loads(row["snapshot"]) if row["snapshot"] else {}
    title = f"{course_name} {row['title']} 締切".strip()
    lines = ([f"課題：{snap['url']}"] if snap.get("url") else []) + [f"Vault：{row['note_path']}"]
    body = _event_body(f"【提出済】{title}" if done else title, due, lines, PROP, row["assignment_id"], done)
    return body, "done" if done else "active"


def _norm(t: dict) -> str:
    if "date" in t:
        return t["date"]
    return datetime.fromisoformat(t["dateTime"]).astimezone(JST).isoformat()


def fingerprint(ev: dict) -> str:
    """本人の編集を見分けるための件名・開始・終了。書式の違い（秒・オフセット）は吸収する。"""
    return json.dumps([ev.get("summary", ""), _norm(ev["start"]), _norm(ev["end"])], ensure_ascii=False)


def _passed(due: datetime | date, now: datetime) -> bool:
    return due <= now if isinstance(due, datetime) else due < now.date()


# ---------------------------------------------------------------- 手書きのタスク行

LINE_RE = re.compile(r"^\s*- \[(?P<status>.)\] (?P<body>.*)$")
SYNC_ID_RE = re.compile(r"<!-- university-sync:id=(?P<id>ua-[0-9a-f]+) -->")
DUE_DATE_RE = re.compile(r"📅 (\d{4})-(\d{2})-(\d{2})")
FIELD_RE = re.compile(r"\s*(?:[📅⏳✅❌➕🛫] \d{4}-\d{2}-\d{2}|[⏫🔼🔽🔺⏬]|🔁 [^📅⏳✅❌]*)")
TAG_RE = re.compile(r"(?<!\S)#[^\s#\d][^\s#]*")  # Obsidian のタグ。「#02」のような数字だけのものはタグではない
EXPLICIT_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s*(\d{1,2}):(\d{2})\s*まで")
TIME_RE = re.compile(r"(?:〜|~)\s*(\d{1,2}):(\d{2})|(\d{1,2}):(\d{2})\s*(?:まで|締切|〆)")


@dataclass
class TaskItem:
    key: str
    status: str          # " " / "x" / "-" など（Tasks の記法）
    title: str           # 予定の件名にする部分
    text: str            # 行の本文（説明欄に載せる）
    due: datetime | date


def _clean_title(body: str) -> str:
    t = SYNC_ID_RE.sub("", body)
    t = FIELD_RE.sub("", t)
    t = TAG_RE.sub("", t)
    t = re.sub(r"\[\[[^\]]*\]\]", "", t)
    t = re.sub(r"[（(].*?[）)]", "", t)  # 補足の括弧書き
    return re.sub(r"\s+", " ", t).strip()


def _line_due(body: str) -> datetime | date | None:
    m = DUE_DATE_RE.search(body)
    if not m:
        return None
    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    text = FIELD_RE.sub("", body)
    e = EXPLICIT_RE.search(text)
    if e:  # 「10/26 00:00まで」のように日付ごと書かれた締切を優先する
        mon, day, hh, mm = map(int, e.groups())
        year = d.year + (1 if mon < d.month - 6 else 0)
        try:
            return datetime(year, mon, day, hh, mm, tzinfo=JST)
        except ValueError:
            pass
    t = TIME_RE.search(text)
    if t:
        hh, mm = (int(x) for x in (t.group(1) or t.group(3), t.group(2) or t.group(4)))
        if hh == 24 and mm == 0:
            return datetime(d.year, d.month, d.day, tzinfo=JST) + timedelta(days=1)
        if hh < 24:
            return datetime(d.year, d.month, d.day, hh, mm, tzinfo=JST)
    return d


def task_items(tasks_text: str, store) -> list[TaskItem]:
    """大学タスクのノートから、カレンダーに載せる手書きの行を拾う。"""
    out, seen = [], set()
    for line in tasks_text.split("\n"):
        m = LINE_RE.match(line)
        if not m:
            continue
        body = m.group("body")
        due = _line_due(body)
        if due is None:
            continue
        sid = SYNC_ID_RE.search(body)
        if sid:
            row = store.assignment(sid.group("id"))
            if row is None or _row_due(row) is not None:
                continue  # 学校側に締切がある管理課題は、管理課題の予定として扱う
            key = "aid:" + sid.group("id")
        else:
            norm = re.sub(r"\s+", " ", FIELD_RE.sub("", body)).strip()
            key = "txt:" + hashlib.sha256(norm.encode()).hexdigest()[:12]
        if key in seen:
            continue
        seen.add(key)
        title = _clean_title(body)
        if title:
            out.append(TaskItem(key, m.group("status"), title, SYNC_ID_RE.sub("", body).strip(), due))
    return out


def task_event(item: TaskItem, tasks_note: str) -> tuple[dict, str] | None:
    if item.status == "-":
        return None  # 取り消し
    done = item.status not in (" ", "/")
    body = _event_body(f"【完了】{item.title}" if done else item.title, item.due,
                       [f"タスク：{item.text}", f"Vault：{tasks_note}（手書きのタスク行から登録）"], TASK_PROP, item.key, done)
    return body, "done" if done else "active"


def _external_same(cal, item: TaskItem) -> bool:
    """同じ日に、本人が手で入れた同名の予定があるか（このツールが作った予定は除く）。"""
    day = item.due.date() if isinstance(item.due, datetime) else item.due
    for ev in cal.on_day(day):
        priv = ev.get("extendedProperties", {}).get("private", {})
        if PROP in priv or TASK_PROP in priv:
            continue
        s = ev.get("summary", "").strip()
        if s and (s in item.title or item.title in s):
            return True
    return False


# ---------------------------------------------------------------- 照合

class _Records:
    """状態ストアの対応表（管理課題用・手書きタスク用）を同じ形で扱う。"""

    def __init__(self, store, kind: str):
        self.store, self.kind = store, kind

    def get(self, key):
        return self.store.calendar_event(key) if self.kind == "aid" else self.store.calendar_task(key)

    def set(self, key, cal_id, event_id, fp, status, title=""):
        if self.kind == "aid":
            self.store.set_calendar_event(key, cal_id, event_id, fp, status, iso_now())
        else:
            self.store.set_calendar_task(key, cal_id, event_id, fp, status, title, iso_now())


def _reconcile(cfg, cal, recs: _Records, report: CalendarReport, *, key: str, label: str, want, may_create: bool,
               prop: str, dry_run: bool, external=None) -> None:
    rec = recs.get(key)
    if rec is not None and rec["calendar_id"] != cfg.calendar_id:
        rec = None  # 登録先のカレンダーを変えた。前のカレンダーの予定には触れない
    if rec is not None and rec["status"] in ("user_deleted", "user_edited", "external"):
        return
    live = rec is not None and rec["status"] in ("active", "done") and rec["event_id"]
    plan = report.planned
    try:
        if want is None:
            if live:
                plan["remove"] += 1
                if not dry_run:
                    cal.delete(rec["event_id"])
                    recs.set(key, cfg.calendar_id, None, None, "removed", label)
                    report.removed += 1
            return
        body, mode = want
        if not live:
            if not may_create:
                return
            if dry_run:
                plan["create"] += 1
                return
            existing = cal.find(prop, key)  # 状態ストアを失った後の再実行でも重複させない
            if existing is None and external is not None and external():
                recs.set(key, cfg.calendar_id, None, None, "external", label)
                return
            plan["create"] += 1
            ev = cal.patch(existing["id"], body) if existing else cal.insert(body)
            recs.set(key, cfg.calendar_id, ev["id"], fingerprint(body), mode, label)
            report.created += 1
            return
        if fingerprint(body) == rec["fingerprint"] and mode == rec["status"]:
            return
        plan["update"] += 1
        if dry_run:
            return
        ev = cal.get(rec["event_id"])
        if ev is None or ev.get("status") == "cancelled":
            recs.set(key, cfg.calendar_id, rec["event_id"], rec["fingerprint"], "user_deleted", label)
            report.reviews.append((f"calendar_deleted:{key}", "カレンダーの予定を本人が削除", key if prop == PROP else None,
                                   f"{label}: 締切の予定が削除されていたので作り直さない（以後この予定は自動で扱わない）"))
            return
        if fingerprint(ev) != rec["fingerprint"]:
            recs.set(key, cfg.calendar_id, rec["event_id"], rec["fingerprint"], "user_edited", label)
            report.reviews.append((f"calendar_edited:{key}", "カレンダーの予定を本人が編集", key if prop == PROP else None,
                                   f"{label}: 締切の予定の件名か時刻が手で変えられていたので上書きしない"))
            return
        cal.patch(rec["event_id"], body)
        recs.set(key, cfg.calendar_id, rec["event_id"], fingerprint(body), mode, label)
        report.updated += 1
    except CollectorError as e:
        report.errors.append(f"{label}: {e}")


def sync_calendar(cfg, store, cal, *, dry_run: bool, now: datetime | None = None, tasks_text: str | None = None) -> CalendarReport:
    """tasks_text（大学タスクのノートの本文）を渡すと、手書きのタスク行も反映する。"""
    now = now or now_jst()
    report = CalendarReport()

    recs = _Records(store, "aid")
    for row in store.all_assignments():
        course = cfg.course_by_id(row["course_id"])
        want = desired_event(row, course.name if course else "")
        due = _row_due(row)
        may = want is not None and want[1] == "active" and not _passed(due, now)
        _reconcile(cfg, cal, recs, report, key=row["assignment_id"], label=row["title"], want=want, may_create=may,
                   prop=PROP, dry_run=dry_run)

    if tasks_text is not None:
        trecs = _Records(store, "task")
        current = set()
        for item in task_items(tasks_text, store):
            current.add(item.key)
            want = task_event(item, cfg.tasks_note)
            may = want is not None and want[1] == "active" and not _passed(item.due, now)
            _reconcile(cfg, cal, trecs, report, key=item.key, label=item.title, want=want, may_create=may,
                       prop=TASK_PROP, dry_run=dry_run, external=lambda item=item: _external_same(cal, item))
        for rec in store.calendar_tasks():
            if rec["task_key"] not in current:  # 行が消えた・書き換えられた・学校側に締切が付いた
                _reconcile(cfg, cal, trecs, report, key=rec["task_key"], label=rec["title"] or rec["task_key"],
                           want=None, may_create=False, prop=TASK_PROP, dry_run=dry_run)

    if report.errors:
        report.ok = False
        report.detail = f"{len(report.errors)} 件を反映できなかった（次回再試行）"
    return report
