"""試験日程の登録と、重なる授業の回の削除（Phase 6）。実際の Google・Claude には触れない。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from test_calendar import EXTRA, FakeCalendar
from test_sync import Env

from university_sync.exams import EXAM_PROP, same_course, sync_exams
from university_sync.state import StateStore
from university_sync.timeutil import JST

CID = "100000000001"  # サンプル科目A
NOW = datetime(2026, 10, 18, 9, 0, tzinfo=JST)
ANN = "講義日程について：\n中間試験は12/2(水)の授業時間に行います。\n期末試験は2/10(水)になります。"


class FakeCollector:
    def __init__(self, anns):
        self.anns = anns

    def announcements_page(self, course_id, token):
        return (self.anns if course_id == CID else []), None


class FakeLLM:
    def __init__(self, out):
        self.out, self.calls = out, 0

    def extract_exams(self, course_name, posted_at, text):
        self.calls += 1
        return self.out


MIDTERM = {"kind": "中間試験", "date": "2026-12-02", "date_raw": "12/2(水)", "start": "", "end": "",
           "in_class_time": True, "evidence_text": "中間試験は12/2(水)の授業時間に行います。"}
FINAL = {"kind": "期末試験", "date": "2027-02-10", "date_raw": "2/10(水)", "start": "", "end": "",
         "in_class_time": False, "evidence_text": "期末試験は2/10(水)になります。"}


def class_event(eid, summary, day, start, end):
    return {"id": eid, "status": "confirmed", "summary": summary,
            "start": {"dateTime": f"{day}T{start}:00+09:00"}, "end": {"dateTime": f"{day}T{end}:00+09:00"}}


class ExamTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.use("sample_v1", EXTRA + '\nclass_calendar_id = "class@group.calendar.google.com"\n'
                     .replace("\n", "\n", 1))
        self.cal, self.classes = FakeCalendar(), FakeCalendar()
        self.classes.events["cls_1202"] = class_event("cls_1202", "サンプル科目A（7限）", "2026-12-02", "19:30", "21:00")
        self.classes.events["cls_0210"] = class_event("cls_0210", "サンプル科目A（7限）", "2027-02-10", "19:30", "21:00")
        self.classes.events["other_1202"] = class_event("other_1202", "別の授業（6限）", "2026-12-02", "17:50", "19:20")
        self.store = StateStore.open(self.env.cfg.state_dir)

    def tearDown(self):
        self.store.close()
        self.env.cleanup()

    def sync(self, llm, anns=None, dry_run=False):
        return sync_exams(self.env.cfg, self.store, self.cal, self.classes,
                          FakeCollector([{"id": "A1", "text": ANN, "creationTime": "2026-10-14T00:00:00Z"}] if anns is None else anns),
                          llm, dry_run=dry_run, now=NOW)

    def exam_events(self):
        return [e for e in self.cal.events.values() if EXAM_PROP in e.get("extendedProperties", {}).get("private", {})]

    def test_in_class_exam_uses_class_time_and_removes_that_class(self):
        r = self.sync(FakeLLM([MIDTERM]))
        self.assertTrue(r.ok, r.errors)
        ev = self.exam_events()[0]
        self.assertEqual(ev["summary"], "サンプル科目A 中間試験")
        self.assertTrue(ev["start"]["dateTime"].startswith("2026-12-02T19:30"))
        self.assertTrue(ev["end"]["dateTime"].startswith("2026-12-02T21:00"))
        self.assertNotIn("cls_1202", self.classes.events)  # その回だけ消える
        self.assertIn("cls_0210", self.classes.events)
        self.assertIn("other_1202", self.classes.events)  # 重ならない別の授業は残る
        self.assertIn("試験のため授業の回を削除", [k for _, k, _, _ in r.reviews])

    def test_date_only_exam_is_all_day_and_removes_nothing(self):
        self.sync(FakeLLM([FINAL]))
        ev = self.exam_events()[0]
        self.assertEqual(ev["start"], {"date": "2027-02-10"})
        self.assertIn("cls_0210", self.classes.events)

    def test_manual_exam_event_is_not_duplicated_but_its_time_removes_class(self):
        self.cal.events["mine"] = class_event("mine", "サンプル科目A 期末試験", "2027-02-10", "19:30", "21:00")
        self.sync(FakeLLM([FINAL]))
        self.assertEqual(self.exam_events(), [])
        self.assertNotIn("cls_0210", self.classes.events)

    def test_overlap_with_other_course_is_reported_not_deleted(self):
        x = dict(MIDTERM, start="18:00", end="19:40", in_class_time=False)
        r = self.sync(FakeLLM([x]))
        self.assertIn("other_1202", self.classes.events)
        self.assertNotIn("cls_1202", self.classes.events)  # 同じ授業の回（19:30〜）とは重なる
        self.assertIn("試験と別の授業が重なる", [k for _, k, _, _ in r.reviews])

    def test_unsupported_extraction_is_dropped(self):
        bad = dict(MIDTERM, evidence_text="本文に無い文", date_raw="12/2(水)")
        self.sync(FakeLLM([bad]))
        self.assertEqual(self.exam_events(), [])
        self.assertIn("cls_1202", self.classes.events)

    def test_same_announcement_read_once_and_idempotent(self):
        llm = FakeLLM([MIDTERM, FINAL])
        self.sync(llm)
        n = len(self.cal.events)
        self.cal.calls.clear()
        self.classes.calls.clear()
        r = self.sync(llm)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(len(self.cal.events), n)
        self.assertEqual([c for c, _ in self.cal.calls + self.classes.calls if c in ("insert", "patch", "delete")], [])
        self.assertEqual(r.classes_removed, [])

    def test_dry_run_reads_nothing_and_changes_nothing(self):
        llm = FakeLLM([MIDTERM])
        r = self.sync(llm, dry_run=True)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(self.exam_events(), [])
        self.assertIn("cls_1202", self.classes.events)
        self.assertTrue(r.ok)

    def test_mail_candidate_exam_is_registered(self):
        data = {"event_type": "exam", "title": "サンプル科目A 中間試験", "course_candidates": ["サンプル科目A"],
                "deadline_candidates": [{"raw": "12月2日 19:30", "date": "2026-12-02", "time": "19:30", "precision": "datetime"}],
                "evidence_text": "中間試験を12月2日 19:30から行います", "needs_review": True, "review_reason": ""}
        self.store.conn.execute("INSERT INTO mail_messages(message_id,status,first_seen_at) VALUES ('m1','done','x')")
        self.store.add_candidate("mc-1", "m1", 0, data, "2026-10-18T00:00:00+09:00")
        self.sync(FakeLLM([]), anns=[])
        ev = self.exam_events()[0]
        self.assertTrue(ev["start"]["dateTime"].startswith("2026-12-02T19:30"))
        self.assertNotIn("cls_1202", self.classes.events)

    def test_course_name_matching(self):
        self.assertTrue(same_course("電気回路学および演習（2限）", "電気回路学"))
        self.assertTrue(same_course("技術課程演習（7限）", "技術課程演習第二"))
        self.assertFalse(same_course("確率統計（7限）", "電気回路学"))


if __name__ == "__main__":
    unittest.main()
