"""締切カレンダーへの登録（Phase 6）。実際の Google カレンダーには触れない。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from test_sync import Env

from university_sync.collectors import CollectorError
from university_sync.collectors.sample import SampleClassroomCollector
from university_sync.gcalendar import PROP, desired_event, fingerprint, sync_calendar
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

    def find(self, aid):
        self.calls.append(("find", aid))
        for ev in self.events.values():
            if ev.get("extendedProperties", {}).get("private", {}).get(PROP) == aid and ev["status"] != "cancelled":
                return ev
        return None

    def insert(self, body):
        self.calls.append(("insert", body["summary"]))
        if self.fail_insert:
            raise CollectorError("HTTP 500", status=500)
        return self._store(f"e{len(self.events) + 1}", body)

    def patch(self, eid, body):
        self.calls.append(("patch", eid))
        merged = dict(self.events[eid]) | body
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
        self.assertEqual((end - start).total_seconds(), 1800)  # 締切の 30 分前〜締切
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

    def test_date_only_due_is_all_day(self):
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


if __name__ == "__main__":
    unittest.main()
