"""締切カレンダーへの登録（Phase 6）。実際の Google カレンダーには触れない。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from test_sync import Env

from university_sync.collectors import CollectorError
from university_sync.collectors.sample import SampleClassroomCollector
from university_sync.gcalendar import PROP, SLOT_PROP, TASK_PROP, desired_event, fingerprint, sync_calendar, task_items
from university_sync.state import StateStore
from university_sync.sync import run
from university_sync.timeutil import JST

CAL = "cal@group.calendar.google.com"
EXTRA = f"""
[accounts.storage]
email = "me@gmail.example"
deadline_calendar_id = "{CAL}"
"""
BEFORE_ALL = datetime(2026, 10, 1, 9, 0, tzinfo=JST)  # サンプルの締切（10/15〜10/21）より前


class FakeCalendar:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_insert = False

    def _store(self, eid, body):
        ev = json.loads(json.dumps(body))
        ev["id"], ev["status"] = eid, "confirmed"
        # Google は日時を秒付き・オフセット付きで返す。書式の違いで本人編集と誤判定しないことを確かめる
        for k in ("start", "end"):
            if "dateTime" in ev[k]:
                ev[k]["dateTime"] = datetime.fromisoformat(ev[k]["dateTime"]).astimezone(JST).isoformat()
        self.events[eid] = ev
        return ev

    def get(self, eid):
        self.calls.append(("get", eid))
        ev = self.events.get(eid)
        return dict(ev) if ev else None

    def find(self, prop, value):
        self.calls.append(("find", value))
        for ev in self.events.values():
            if ev.get("extendedProperties", {}).get("private", {}).get(prop) == value and ev["status"] != "cancelled":
                return ev
        return None

    def on_day(self, day):
        self.calls.append(("on_day", str(day)))
        out = []
        for ev in self.events.values():
            d = ev["start"].get("date") or ev["start"]["dateTime"][:10]
            if d == day.isoformat():
                out.append(ev)
        return out

    def insert(self, body):
        self.calls.append(("insert", body["summary"]))
        if self.fail_insert:
            raise CollectorError("HTTP 500", status=500)
        return self._store(f"e{len(self.events) + 1}", body)

    def patch(self, eid, body):
        """Google の patch と同じく start / end は項目ごとに上書きし、null の項目は消す。"""
        from university_sync.gcalendar import _patch_body
        self.calls.append(("patch", eid))
        body = _patch_body(body)
        merged = dict(self.events[eid])
        for k, v in body.items():
            if k in ("start", "end"):
                t = dict(merged.get(k, {})) | v
                t = {a: b for a, b in t.items() if b is not None}
                if "date" in t and "dateTime" in t:
                    raise CollectorError("HTTP 400 Invalid start time.", status=400)
                merged[k] = t
            else:
                merged[k] = v
        return self._store(eid, merged)

    def delete(self, eid):
        self.calls.append(("delete", eid))
        self.events.pop(eid, None)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.use("sample_v1", EXTRA)
        self.cal = FakeCalendar()
        run(self.env.cfg, SampleClassroomCollector(self.env.cfg.sample_dir), dry_run=False)  # 課題を状態ストアへ

    def tearDown(self):
        self.env.cleanup()

    def sync(self, now=BEFORE_ALL, dry_run=False):
        store = StateStore.open(self.env.cfg.state_dir, read_only=dry_run)
        try:
            return sync_calendar(self.env.cfg, store, self.cal, dry_run=dry_run, now=now)
        finally:
            store.close()

    def store(self):
        return StateStore.open(self.env.cfg.state_dir)

    def rows_with_due(self):
        """締切があり未提出の課題（予定を作る対象）。サンプルには提出済みの課題も 1 件ある。"""
        s = self.store()
        try:
            return [r for r in s.all_assignments() if r["due_at"] and r["submission_state"] not in ("TURNED_IN", "RETURNED")]
        finally:
            s.close()

    def set_row(self, aid, **fields):
        s = self.store()
        try:
            cols = ",".join(f"{k}=?" for k in fields)
            s.conn.execute(f"UPDATE assignments SET {cols} WHERE assignment_id=?", [*fields.values(), aid])
            s.conn.commit()
        finally:
            s.close()

    def test_creates_events_in_existing_style(self):
        r = self.sync()
        self.assertEqual(r.created, len(self.rows_with_due()))
        self.assertTrue(r.ok)
        timed = [e for e in self.cal.events.values() if "dateTime" in e["start"]]
        self.assertTrue(timed)
        ev = timed[0]
        start, end = (datetime.fromisoformat(ev[k]["dateTime"]) for k in ("start", "end"))
        self.assertEqual((end - start).total_seconds(), 3600)  # 締切の 1 時間前〜締切
        self.assertEqual(ev["reminders"]["overrides"], [{"method": "popup", "minutes": 1440}, {"method": "popup", "minutes": 30}])
        self.assertTrue(ev["summary"].startswith("サンプル科目A ") and ev["summary"].endswith(" 締切"))
        self.assertIn("締切時刻：", ev["description"])

    def test_idempotent_no_api_calls_when_unchanged(self):
        self.sync()
        self.cal.calls.clear()
        r = self.sync()
        self.assertEqual((r.created, r.updated, r.removed), (0, 0, 0))
        self.assertEqual(self.cal.calls, [])

    def test_due_change_moves_event(self):
        self.sync()
        row = next(r for r in self.rows_with_due() if r["due_precision"] == "datetime")
        self.set_row(row["assignment_id"], due_at="2026-10-30T23:59+09:00")
        r = self.sync()
        self.assertEqual(r.updated, 1)
        ev = next(e for e in self.cal.events.values() if e["extendedProperties"]["private"][PROP] == row["assignment_id"])
        self.assertTrue(ev["end"]["dateTime"].startswith("2026-10-30T23:59"))

    def test_turned_in_marks_done_without_reminders(self):
        self.sync()
        row = self.rows_with_due()[0]
        self.set_row(row["assignment_id"], submission_state="TURNED_IN")
        self.assertEqual(self.sync().updated, 1)
        ev = next(e for e in self.cal.events.values() if e["extendedProperties"]["private"][PROP] == row["assignment_id"])
        self.assertTrue(ev["summary"].startswith("【提出済】"))
        self.assertEqual(ev["reminders"]["overrides"], [])

    def test_due_removed_deletes_event(self):
        self.sync()
        row = self.rows_with_due()[0]
        n = len(self.cal.events)
        self.set_row(row["assignment_id"], due_at=None, due_precision="unknown")
        self.assertEqual(self.sync().removed, 1)
        self.assertEqual(len(self.cal.events), n - 1)

    def test_user_edit_is_not_overwritten(self):
        self.sync()
        row = next(r for r in self.rows_with_due() if r["due_precision"] == "datetime")
        ev = next(e for e in self.cal.events.values() if e["extendedProperties"]["private"][PROP] == row["assignment_id"])
        ev["summary"] = "自分で直した件名"
        self.set_row(row["assignment_id"], due_at="2026-10-30T23:59+09:00")
        r = self.sync()
        self.assertEqual(r.updated, 0)
        self.assertEqual(ev["summary"], "自分で直した件名")
        self.assertEqual([k for _, k, _, _ in r.reviews], ["カレンダーの予定を本人が編集"])
        self.cal.calls.clear()
        self.sync()
        self.assertEqual(self.cal.calls, [])  # 以後その課題には触れない

    def test_user_deleted_event_is_not_recreated(self):
        self.sync()
        row = next(r for r in self.rows_with_due() if r["due_precision"] == "datetime")
        eid = next(k for k, e in self.cal.events.items() if e["extendedProperties"]["private"][PROP] == row["assignment_id"])
        del self.cal.events[eid]
        self.set_row(row["assignment_id"], due_at="2026-10-30T23:59+09:00")
        r = self.sync()
        self.assertEqual((r.created, r.updated), (0, 0))
        self.assertEqual([k for _, k, _, _ in r.reviews], ["カレンダーの予定を本人が削除"])

    def test_past_due_or_done_not_created(self):
        after = datetime(2026, 12, 1, tzinfo=JST)
        self.assertEqual(self.sync(now=after).created, 0)
        self.assertEqual(self.cal.events, {})

    def test_lost_state_does_not_duplicate(self):
        self.sync()
        n = len(self.cal.events)
        s = self.store()
        s.conn.execute("DELETE FROM calendar_events")
        s.conn.commit()
        s.close()
        r = self.sync()
        self.assertEqual(len(self.cal.events), n)
        self.assertNotIn("insert", [c for c, _ in self.cal.calls[-r.created * 2:]])

    def test_dry_run_calls_nothing(self):
        r = self.sync(dry_run=True)
        self.assertEqual(r.planned["create"], len(self.rows_with_due()))
        self.assertEqual(self.cal.calls, [])
        self.assertEqual(self.cal.events, {})

    def test_failure_is_reported_and_retried(self):
        self.cal.fail_insert = True
        r = self.sync()
        self.assertFalse(r.ok)
        self.assertEqual(r.created, 0)
        self.cal.fail_insert = False
        self.assertEqual(self.sync().created, len(self.rows_with_due()))

    def test_date_only_due_is_all_day_assignment(self):
        row = self.rows_with_due()[0]
        body, mode = desired_event(dict(row) | {"due_at": "2026-10-20", "due_precision": "date"}, "科目", BEFORE_ALL)
        self.assertEqual(body["start"], {"date": "2026-10-20"})
        self.assertEqual(body["end"], {"date": "2026-10-21"})
        self.assertEqual(mode, "active")
        self.assertIn("時刻の指定なし", body["description"])

    def test_fingerprint_ignores_format(self):
        a = {"summary": "x", "start": {"dateTime": "2026-10-20T23:29:00+09:00"}, "end": {"dateTime": "2026-10-20T23:59:00+09:00"}}
        b = {"summary": "x", "start": {"dateTime": "2026-10-20T14:29:00Z"}, "end": {"dateTime": "2026-10-20T14:59:00Z"}}
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_run_wires_calendar_stage(self):
        plan, results = run(self.env.cfg, SampleClassroomCollector(self.env.cfg.sample_dir), dry_run=False,
                            calendar_factory=lambda store: self.cal)
        self.assertIsNotNone(plan.calendar)
        self.assertTrue(plan.calendar.ok)
        status = (self.env.vault / "Areas/University/連携管理/同期状況.md").read_text(encoding="utf-8")
        self.assertIn("calendar.deadlines", status)

    def test_factory_failure_does_not_stop_sync(self):
        def broken(store):
            raise CollectorError("追加の権限が必要です", permanent=True)
        plan, results = run(self.env.cfg, SampleClassroomCollector(self.env.cfg.sample_dir), dry_run=False,
                            calendar_factory=broken)
        self.assertFalse(plan.calendar.ok)
        self.assertEqual(results["status"], "partial")


MANUAL = """
- [ ] 基礎プログラミング #02 activity-report・assignment提出（〜23:59・[[資料一覧|資料一覧]]） #university 📅 2026-10-18
- [ ] 基礎プログラミング #01 assignment late-submission提出（10/26 00:00まで。通常提出済みなら不要） #university 📅 2026-10-25
- [ ] 確率統計 小テスト第2回提出（講義開始19:30まで・手書き） #university ⏫ 📅 2026-10-21
- [ ] 確率統計 中間試験（水7限・前半範囲） #university 📅 2026-12-02
- [ ] 時刻のないタスク #university 📅 2026-10-30
- [x] 終わったタスク #university 📅 2026-10-28 ✅ 2026-10-17
- [ ] 締切のないタスク #university
"""


class ManualTaskTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.use("sample_v1", EXTRA)
        self.cal = FakeCalendar()
        self.store = StateStore.open(self.env.cfg.state_dir)
        self.now = datetime(2026, 10, 18, 3, 0, tzinfo=JST)

    def tearDown(self):
        self.store.close()
        self.env.cleanup()

    def sync(self, text=MANUAL, now=None):
        return sync_calendar(self.env.cfg, self.store, self.cal, dry_run=False, now=now or self.now, tasks_text=text)

    def by_title(self, part):
        return [e for e in self.cal.events.values() if part in e["summary"]]

    def test_times_are_read_from_text(self):
        items = {i.title: i.due for i in task_items(MANUAL, self.store)}
        self.assertEqual(items["基礎プログラミング #02 activity-report・assignment提出"], datetime(2026, 10, 18, 23, 59, tzinfo=JST))
        self.assertEqual(items["基礎プログラミング #01 assignment late-submission提出"], datetime(2026, 10, 26, 0, 0, tzinfo=JST))
        self.assertEqual(items["確率統計 小テスト第2回提出"], datetime(2026, 10, 21, 19, 30, tzinfo=JST))
        self.assertEqual(str(items["時刻のないタスク"]), "2026-10-30")
        self.assertNotIn("締切のないタスク", items)

    def test_creates_open_future_tasks_only(self):
        r = self.sync()
        self.assertTrue(r.ok)
        titles = sorted(e["summary"] for e in self.cal.events.values())
        self.assertIn("基礎プログラミング #02 activity-report・assignment提出", titles)
        self.assertNotIn("終わったタスク", " ".join(titles))
        allday = self.by_title("時刻のないタスク")[0]
        self.assertEqual(allday["start"], {"date": "2026-10-30"})
        self.assertTrue(all(e["extendedProperties"]["private"].get(TASK_PROP) for e in self.cal.events.values()))

    def test_existing_manual_event_is_not_duplicated(self):
        self.cal.events["mine"] = {"id": "mine", "status": "confirmed", "summary": "確率統計 中間試験",
                                   "start": {"dateTime": "2026-12-02T19:30:00+09:00"}, "end": {"dateTime": "2026-12-02T21:00:00+09:00"}}
        self.sync()
        self.assertEqual(len(self.by_title("中間試験")), 1)
        self.cal.calls.clear()
        self.sync()
        self.assertNotIn(("on_day", "2026-12-02"), self.cal.calls)  # 一度「本人の予定あり」と判断したら毎回は探さない

    def test_check_marks_done_and_due_change_moves(self):
        self.sync()
        text = MANUAL.replace("- [ ] 基礎プログラミング #02", "- [x] 基礎プログラミング #02")
        text = text.replace("小テスト第2回提出（講義開始19:30まで・手書き） #university ⏫ 📅 2026-10-21",
                            "小テスト第2回提出（講義開始19:30まで・手書き） #university ⏫ 📅 2026-10-28")
        r = self.sync(text)
        self.assertEqual(r.updated, 2)
        done = self.by_title("#02")[0]
        self.assertTrue(done["summary"].startswith("【完了】"))
        self.assertEqual(done["reminders"]["overrides"], [])
        self.assertTrue(self.by_title("小テスト")[0]["end"]["dateTime"].startswith("2026-10-28T19:30"))

    def test_removed_or_cancelled_line_deletes_event(self):
        self.sync()
        text = MANUAL.replace("- [ ] 時刻のないタスク", "- [-] 時刻のないタスク")
        text = "\n".join(l for l in text.split("\n") if "#02" not in l)
        r = self.sync(text)
        self.assertEqual(r.removed, 2)
        self.assertEqual(self.by_title("時刻のないタスク"), [])
        self.assertEqual(self.by_title("#02"), [])

    def test_idempotent(self):
        self.sync()
        self.cal.calls.clear()
        r = self.sync()
        self.assertEqual((r.created, r.updated, r.removed), (0, 0, 0))
        self.assertEqual(self.cal.calls, [])

    def test_all_day_becomes_timed_when_time_added(self):
        self.sync()
        text = MANUAL.replace("時刻のないタスク #university", "時刻のないタスク（〜23:59） #university")
        r = self.sync(text)
        self.assertTrue(r.ok, r.errors)
        ev = self.by_title("時刻のないタスク")
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["end"]["dateTime"].startswith("2026-10-30T23:59"))
        self.assertNotIn("date", ev[0]["start"])

    def test_managed_line_without_school_due_uses_line_date(self):
        run(self.env.cfg, SampleClassroomCollector(self.env.cfg.sample_dir), dry_run=False)
        self.store.close()
        self.store = StateStore.open(self.env.cfg.state_dir)
        row = self.store.all_assignments()[0]
        self.store.conn.execute("UPDATE assignments SET due_at=NULL, due_precision='unknown' WHERE assignment_id=?",
                                (row["assignment_id"],))
        self.store.conn.commit()
        text = f"- [ ] 実験レポート提出（締切未設定） [[ノート]] <!-- university-sync:id={row['assignment_id']} --> #university 📅 2026-10-31"
        self.sync(text)
        ev = self.by_title("実験レポート提出")[0]
        self.assertEqual(ev["start"], {"date": "2026-10-31"})
        self.assertEqual(ev["extendedProperties"]["private"][TASK_PROP], "aid:" + row["assignment_id"])
        # 同じ行に時刻を書き足すと、同じ予定のまま終日から時刻ありに変わる（patch で date を消す）
        r = self.sync(text.replace("--> #university", "--> （〜23:59） #university"))
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.updated, 1)
        ev = self.by_title("実験レポート提出")
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["end"]["dateTime"].startswith("2026-10-31T23:59"))
        self.assertNotIn("date", ev[0]["start"])


SLOTS = EXTRA + """
[[calendar_slots]]
course_id = "100000000001"
title = "サンプル科目A レポート（見込み）"
deadlines = ["2026-10-21T23:59", "2026-10-28T23:59", "2026-10-01T23:59"]
"""


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.use("sample_v1", SLOTS)
        self.cal = FakeCalendar()
        self.now = datetime(2026, 10, 10, 9, 0, tzinfo=JST)

    def tearDown(self):
        self.env.cleanup()

    def sync(self):
        store = StateStore.open(self.env.cfg.state_dir)
        try:
            return sync_calendar(self.env.cfg, store, self.cal, dry_run=False, now=self.now)
        finally:
            store.close()

    def slot_events(self):
        return [e for e in self.cal.events.values() if SLOT_PROP in e["extendedProperties"]["private"]
                and PROP not in e["extendedProperties"]["private"]]

    def test_future_slots_created_past_skipped(self):
        r = self.sync()
        self.assertEqual(r.created, 2)
        self.assertEqual(sorted(e["end"]["dateTime"][:10] for e in self.slot_events()), ["2026-10-21", "2026-10-28"])
        self.assertIn("見込み", self.slot_events()[0]["description"])

    def test_classroom_assignment_takes_over_nearby_slot(self):
        self.sync()
        slot_id = next(e["id"] for e in self.slot_events() if e["end"]["dateTime"].startswith("2026-10-21"))
        run(self.env.cfg, SampleClassroomCollector(self.env.cfg.sample_dir), dry_run=False)  # 10/20 締切の課題が出る
        self.sync()
        adopted = self.cal.events[slot_id]  # 同じ予定のまま課題の予定になる
        self.assertIn(PROP, adopted["extendedProperties"]["private"])
        self.assertTrue(adopted["end"]["dateTime"].startswith("2026-10-20T23:59"))  # 課題の締切に動いた
        self.assertNotIn("見込み", adopted["summary"])
        self.assertEqual([e["end"]["dateTime"][:10] for e in self.slot_events()], ["2026-10-28"])
        n = len(self.cal.events)
        self.cal.calls.clear()
        self.sync()
        self.assertEqual(len(self.cal.events), n)  # 見込みを作り直さない
        self.assertEqual([c for c, _ in self.cal.calls if c in ("insert", "patch")], [])

    def test_note_change_updates_description(self):
        self.sync()
        self.env.use("sample_v1", SLOTS.replace('title = "サンプル科目A レポート（見込み）"',
                                                'title = "サンプル科目A レポート（見込み）"\nnote = "提出場所：〇〇"'))
        r = self.sync()
        self.assertEqual(r.updated, 2)
        self.assertTrue(all("提出場所：〇〇" in e["description"] for e in self.slot_events()))
        self.assertEqual(r.reviews, [])  # 説明欄の変更は本人編集とみなさない

    def test_slot_removed_from_config_is_deleted(self):
        self.sync()
        self.env.use("sample_v1", SLOTS.replace('"2026-10-28T23:59", ', ""))
        r = self.sync()
        self.assertEqual(r.removed, 1)
        self.assertEqual([e["end"]["dateTime"][:10] for e in self.slot_events()], ["2026-10-21"])


if __name__ == "__main__":
    unittest.main()
