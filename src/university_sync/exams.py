"""試験日程を締切カレンダーへ登録し、重なる授業の回を授業カレンダーから消す（Phase 6）。

1. 見つける：Classroom のお知らせ（試験らしい語を含むものだけ Claude に渡す）と、
   学校メールから抽出済みの候補（種類が exam のもの）から、日付の決まった試験を拾って状態ストアに残す。
   根拠の一節と日付表現が本文に無い抽出結果は捨てる。
2. 時刻を決める：本文の時刻 → 締切カレンダーに本人が手で入れた同名の試験の予定の時刻 →
   「授業時間に行う」と書かれていれば、その日のその授業の時間 → どれも無ければ終日。
3. 登録する：締切カレンダーに「〈授業名〉 〈試験〉」を作る（通知は前日）。本人が同じ日に同名の予定を
   入れていれば作らない。予定には試験の ID を拡張プロパティで持たせ、重複させない。
4. 授業を消す：試験の時間帯と重なる、同じ授業の回（繰り返しの 1 回分だけ）を授業カレンダーから消す。
   別の授業と重なっていたら消さずに要確認へ出す。消した回は状態ストアと要確認一覧に残す。
- 終日（時刻不明）の試験では授業を消さない。過ぎた試験には何もしない。
- dry-run では Claude も呼ばず、カレンダーも変えない。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .collectors import CollectorError, paginate
from .gcalendar import (CalendarReport, PROP, SLOT_PROP, TASK_PROP, _Records, _event_body, _reconcile)
from .llm import LLMError
from .timeutil import JST, iso_now, now_jst
from .vault import sha

EXAM_PROP = "usync_exam"
KEYWORDS = re.compile(r"試験|テスト|中間|期末|exam", re.I)
EXAM_REMINDERS = [{"method": "popup", "minutes": 1440}]
DEFAULT_LENGTH = timedelta(minutes=90)


@dataclass
class ExamReport:
    ok: bool = True
    detail: str = ""
    found: int = 0
    llm_calls: int = 0
    classes_removed: list[str] = field(default_factory=list)
    calendar: CalendarReport = field(default_factory=CalendarReport)
    reviews: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _norm_course(s: str) -> str:
    s = re.sub(r"[（(].*?[）)]", "", s)
    for w in ("および演習", "及び演習", "第二", "第一", "第三", "Ⅱ", "II", "Ⅰ"):
        s = s.replace(w, "")
    return re.sub(r"\s+", "", s)


def same_course(a: str, b: str) -> bool:
    x, y = _norm_course(a), _norm_course(b)
    return len(min(x, y, key=len)) >= 3 and (x in y or y in x)


def _flat(s: str) -> str:
    return "".join(s.split())


# ---------------------------------------------------------------- 見つける

def _valid_time(t: str) -> str | None:
    return t if t and re.fullmatch(r"\d{2}:\d{2}", t) and int(t[:2]) < 24 else None


def collect_from_announcements(cfg, store, collector, llm, report: ExamReport, now: str) -> None:
    for course in cfg.courses:
        if not course.vault_dir:
            continue
        try:
            anns = list(paginate(lambda t: collector.announcements_page(course.course_id, t)))
        except CollectorError as e:
            report.errors.append(f"{course.name} のお知らせ: {e}")
            continue
        for a in anns:
            text = a.get("text", "")
            if not text or not KEYWORDS.search(text):
                continue
            mark = f"exam_ann:{a['id']}"
            if store.kv_get(mark) == sha(text):
                continue  # 同じ本文は一度だけ読む
            try:
                found = llm.extract_exams(course.name, a.get("creationTime", ""), text)
                report.llm_calls += 1
            except LLMError as e:
                report.errors.append(f"{course.name} のお知らせの読み取り: {e}")
                continue
            flat = _flat(text)
            for x in found:
                try:
                    d = date.fromisoformat(x.get("date", ""))
                except ValueError:
                    continue
                if not x.get("date_raw") or _flat(x["date_raw"]) not in flat:
                    continue
                if not x.get("evidence_text") or _flat(x["evidence_text"]) not in flat:
                    continue
                kind = " ".join(x.get("kind", "").split()) or "試験"
                store.upsert_exam({
                    "exam_key": f"exam:{course.course_id}:{d.isoformat()}:{kind}", "course_id": course.course_id,
                    "kind": kind, "exam_date": d.isoformat(), "start_time": _valid_time(x.get("start", "")),
                    "end_time": _valid_time(x.get("end", "")), "in_class": 1 if x.get("in_class_time") else 0,
                    "source": f"classroom-announcement:{a['id']}", "evidence": x["evidence_text"], "first_seen_at": now,
                })
                report.found += 1
            store.kv_set(mark, sha(text))


def collect_from_mail(cfg, store, report: ExamReport, now: str) -> None:
    """メールから抽出済みの候補のうち、試験で授業と日付が一つに決まるもの。本人が閉じた候補は使わない。"""
    rows = store.conn.execute("SELECT * FROM mail_candidates WHERE status != 'dismissed'").fetchall()
    for r in rows:
        c = json.loads(r["data"])
        if c.get("event_type") != "exam" or len(c.get("course_candidates", [])) != 1 or len(c.get("deadline_candidates", [])) != 1:
            continue
        course = next((x for x in cfg.courses if x.name == c["course_candidates"][0]), None)
        if course is None:
            continue
        d = c["deadline_candidates"][0]
        kind = " ".join(c.get("title", "").replace(course.name, "").split()) or "試験"
        store.upsert_exam({
            "exam_key": f"exam:{course.course_id}:{d['date']}:{kind}", "course_id": course.course_id, "kind": kind,
            "exam_date": d["date"], "start_time": _valid_time(d.get("time", "")), "end_time": None, "in_class": 0,
            "source": f"mail:{r['candidate_id']}", "evidence": c.get("evidence_text", ""), "first_seen_at": now,
        })


# ---------------------------------------------------------------- 時刻を決める

def _ours(ev: dict) -> bool:
    priv = ev.get("extendedProperties", {}).get("private", {})
    return any(k in priv for k in (PROP, TASK_PROP, SLOT_PROP, EXAM_PROP))


def _span(ev: dict) -> tuple[datetime, datetime] | None:
    if "dateTime" not in ev.get("start", {}) or "dateTime" not in ev.get("end", {}):
        return None
    return (datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(JST),
            datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(JST))


def _kind_word(kind: str) -> str:
    m = re.search(r"中間|期末|小テスト|追試|再試", kind)
    return m.group(0) if m else kind


def _manual_exam_event(cal, day: date, course_name: str, kind: str) -> dict | None:
    for ev in cal.on_day(day):
        s = ev.get("summary", "")
        if not _ours(ev) and _kind_word(kind) in s and same_course(s, course_name):
            return ev
    return None


def _class_events(class_cal, day: date, course_name: str) -> list[dict]:
    return [ev for ev in class_cal.on_day(day) if same_course(ev.get("summary", ""), course_name) and _span(ev)]


def resolve_time(exam, course_name: str, cal, class_cal) -> tuple[datetime | date, dict | None]:
    """(試験の時間, 本人が入れた同名の予定)。時刻がわからなければ日付だけを返す。"""
    day = date.fromisoformat(exam["exam_date"])
    manual = _manual_exam_event(cal, day, course_name, exam["kind"])
    if exam["start_time"]:
        h, m = map(int, exam["start_time"].split(":"))
        start = datetime(day.year, day.month, day.day, h, m, tzinfo=JST)
        if exam["end_time"]:
            h2, m2 = map(int, exam["end_time"].split(":"))
            end = datetime(day.year, day.month, day.day, h2, m2, tzinfo=JST)
        else:
            end = start + DEFAULT_LENGTH
        return (start, end), manual
    if manual and _span(manual):
        return _span(manual), manual
    if exam["in_class"] and class_cal is not None:
        spans = [_span(ev) for ev in _class_events(class_cal, day, course_name)]
        if spans:
            return (min(s for s, _ in spans), max(e for _, e in spans)), manual
    return day, manual


def exam_body(exam, course_name: str, when) -> dict:
    title = exam["kind"] if course_name and course_name in exam["kind"] else f"{course_name} {exam['kind']}".strip()
    src = "Classroom のお知らせ" if exam["source"].startswith("classroom") else "学校メール"
    lines = [f"試験（{src}より）", f"根拠：{exam['evidence']}" if exam["evidence"] else ""]
    if isinstance(when, tuple):
        start, end = when
        body = _event_body(title, end, [l for l in lines if l], EXAM_PROP, exam["exam_key"], False)
        body["start"] = {"dateTime": start.isoformat(timespec="seconds"), "timeZone": "Asia/Tokyo"}
        body["description"] = body["description"].replace("締切時刻：", "終了時刻：")
    else:
        body = _event_body(title, when, [l for l in lines if l], EXAM_PROP, exam["exam_key"], False)
        body["description"] = body["description"].replace("締切日：", "実施日：")
    body["reminders"] = {"useDefault": False, "overrides": EXAM_REMINDERS}
    body.pop("transparency", None)  # 試験は予定ありとして表示する
    return body


# ---------------------------------------------------------------- 授業を消す

def remove_overlapping_classes(cfg, store, class_cal, exam, course_name: str, when, report: ExamReport, dry_run: bool) -> None:
    if not isinstance(when, tuple) or class_cal is None:
        return  # 時刻がわからない試験では消さない
    start, end = when
    day = start.date()
    for ev in class_cal.on_day(day):
        span = _span(ev)
        if span is None or not (span[0] < end and start < span[1]):
            continue
        if store.class_removal(ev["id"]):
            continue
        summary = ev.get("summary", "")
        label = f"{summary}（{span[0]:%m/%d %H:%M}〜{span[1]:%H:%M}）"
        if not same_course(summary, course_name):
            report.reviews.append((f"exam_overlap:{exam['exam_key']}:{ev['id']}", "試験と別の授業が重なる", None,
                                   f"{course_name} {exam['kind']}（{start:%m/%d %H:%M}〜{end:%H:%M}）が {label} と重なる。"
                                   "別の授業なので授業カレンダーからは消していない"))
            continue
        if dry_run:
            report.classes_removed.append(label)
            continue
        class_cal.delete(ev["id"])  # 繰り返しの 1 回分だけを消す（ID が回ごとに違う）
        store.add_class_removal(ev["id"], exam["exam_key"], summary, span[0].isoformat(), span[1].isoformat(), iso_now())
        report.classes_removed.append(label)
        report.reviews.append((f"class_removed:{ev['id']}", "試験のため授業の回を削除", None,
                               f"{course_name} {exam['kind']}と重なる {label} を授業カレンダーから削除した"
                               "（戻すときは授業カレンダーでその回を作り直す）"))


# ---------------------------------------------------------------- 入口

def sync_exams(cfg, store, cal, class_cal, collector, llm, *, dry_run: bool, now: datetime | None = None) -> ExamReport:
    now = now or now_jst()
    report = ExamReport()
    if not dry_run:
        if collector is not None and llm is not None:
            collect_from_announcements(cfg, store, collector, llm, report, iso_now())
        collect_from_mail(cfg, store, report, iso_now())
    recs = _Records(store, "task")
    for exam in store.all_exams():
        course = cfg.course_by_id(exam["course_id"])
        name = course.name if course else ""
        day = date.fromisoformat(exam["exam_date"])
        if day < now.date():
            continue
        try:
            when, manual = resolve_time(exam, name, cal, class_cal)
        except CollectorError as e:
            report.errors.append(f"{name} {exam['kind']}: {e}")
            continue
        if isinstance(when, tuple) and not exam["start_time"] and not dry_run:
            store.set_exam_time(exam["exam_key"], f"{when[0]:%H:%M}", f"{when[1]:%H:%M}")
        rec = recs.get(exam["exam_key"])
        if manual is not None and (rec is None or rec["status"] == "removed"):
            if not dry_run:
                recs.set(exam["exam_key"], cfg.calendar_id, None, None, "external", f"{name} {exam['kind']}")
        else:
            _reconcile(cfg, cal, recs, report.calendar, key=exam["exam_key"], label=f"{name} {exam['kind']}",
                       want=(exam_body(exam, name, when), "active"), may_create=True, prop=EXAM_PROP, dry_run=dry_run)
        try:
            remove_overlapping_classes(cfg, store, class_cal, exam, name, when, report, dry_run)
        except CollectorError as e:
            report.errors.append(f"{name} {exam['kind']} の授業の削除: {e}")
    report.reviews.extend(report.calendar.reviews)
    report.errors.extend(report.calendar.errors)
    if report.errors:
        report.ok = False
        report.detail = f"{len(report.errors)} 件を反映できなかった（次回再試行）"
    return report
