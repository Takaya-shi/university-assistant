"""Phase 3（メール・Claude・Drive）の検証。実際の Gmail・Claude・Drive には触れない。"""

from __future__ import annotations

import base64
import json
import unittest

from test_sync import Env

from university_sync.collectors import CollectorError
from university_sync.collectors.gmail import HistoryExpired
from university_sync.llm import ClaudeCliAdapter, LLMError, validate
from university_sync.mail import classroom_refs, parse
from university_sync.state import StateStore
from university_sync.sync import accept_candidate, run

MAIL_EXTRA = """
[accounts.school]
email = "me@gl.example.ac.jp"
[mail]
sender_domains = ["example.ac.jp", "classroom.google.com"]
exclude_senders = ["career@office.example.ac.jp"]
initial_range_days = 14
[llm]
mode = "claude-cli"
"""


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def message(mid, sender, subject, body, *, thread="t1", labels=("INBOX",), attachments=(), ts=1760000000000):
    parts = [{"mimeType": "text/plain", "body": {"data": b64(body)}}]
    parts += [{"mimeType": "application/pdf", "filename": f, "body": {"attachmentId": f"att-{f}", "size": 10}} for f in attachments]
    return {"id": mid, "threadId": thread, "labelIds": list(labels), "internalDate": str(ts),
            "payload": {"mimeType": "multipart/mixed", "headers": [{"name": "From", "value": sender},
                                                                   {"name": "Subject", "value": subject}], "parts": parts}}


class FakeGmail:
    account_id = "sample-account"  # Classroom の source_key と同じアカウント
    email = "me@gl.example.ac.jp"

    def __init__(self):
        self.messages = {}
        self.history_queue = []
        self.expired = False
        self.history_id = "100"
        self.gets = 0

    def add(self, m):
        self.messages[m["id"]] = m
        self.history_queue.append(m["id"])

    def current_history_id(self):
        return self.history_id

    def search_ids(self, query):
        self.last_query = query
        return list(self.messages)

    def added_since(self, hid):
        if self.expired:
            raise HistoryExpired("expired", status=404)
        ids, self.history_queue = self.history_queue, []
        self.history_id = str(int(self.history_id) + 1)
        return ids, self.history_id

    def get(self, mid):
        self.gets += 1
        return self.messages[mid]

    def attachment(self, mid, aid):
        return b"%PDF"


class FakeLLM:
    def __init__(self, result=None, fail=False):
        self.result = result
        self.fail = fail
        self.calls = 0

    def extract(self, mail, names):
        self.calls += 1
        if self.fail:
            raise LLMError("fake failure")
        return self.result(mail) if callable(self.result) else self.result


class FakeDrive:
    def __init__(self):
        self.uploaded = []

    def folder_for(self, year, term, course):
        self.folder = (year, term, course)
        return "folder-1"

    def upload(self, folder, name, mime, data, desc):
        self.uploaded.append((folder, name))
        return {"id": f"d-{name}", "webViewLink": f"https://drive.google.com/file/d/d-{name}/view"}


def cand(title, raw, d, t="", courses=("サンプル科目A",), evidence=None, et="assignment"):
    return {"event_type": et, "course_candidates": list(courses), "title": title,
            "deadline_candidates": [{"raw": raw, "date": d, "time": t}] if raw else [],
            "evidence_text": evidence if evidence is not None else raw, "needs_review": False, "review_reason": ""}


class MailTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.use("sample_v1", MAIL_EXTRA)
        self.gmail = FakeGmail()
        self.llm = FakeLLM([])
        self.drive = FakeDrive()

    def tearDown(self):
        self.env.cleanup()

    def run_sync(self, dry_run=False, drive=True, llm=None):
        factory = lambda store: (self.gmail, llm or self.llm, self.drive if drive else None)
        return run(self.env.cfg, __import__("university_sync.collectors.sample", fromlist=["x"]).SampleClassroomCollector(
            self.env.cfg.sample_dir), dry_run=dry_run, mail_factory=factory)

    def store(self):
        return StateStore.open(self.env.state, read_only=True)

    def candidates(self):
        s = self.store()
        try:
            return [dict(r) | {"data": json.loads(r["data"])} for r in s.open_candidates()]
        finally:
            s.close()

    def test_classroom_notification_links_without_llm_or_duplicate(self):
        self.run_sync()  # 先に Classroom の課題を登録
        link = f"https://classroom.google.com/c/{b64('100000000001')}/a/{b64('w1')}/details"
        # w1 は数字でない ID なので、数字 ID の課題で確かめる
        self.assertEqual(classroom_refs(link), [])
        link = f"https://classroom.google.com/c/{b64('100000000001')}/sa/{b64('123')}/details"
        self.assertEqual(classroom_refs(f"新しい課題 {link}"), [("100000000001", "123")])
        self.gmail.add(message("m1", "Google Classroom <no-reply@classroom.google.com>", "新しい課題", f"課題 {link}"))
        before = len(self.env.task_lines())
        plan, results = self.run_sync()
        self.assertEqual(self.llm.calls, 0)
        self.assertEqual(self.candidates(), [])
        self.assertEqual(len(self.env.task_lines()), before)

    def test_multiple_assignments_in_one_mail_split(self):
        body = "第4回レポートは10月20日23:59締切。\n第5回レポートは10月27日締切。"
        self.gmail.add(message("m2", "教員 <prof@example.ac.jp>", "レポートについて", body))
        self.llm.result = [cand("第4回レポート", "10月20日23:59", "2026-10-20", "23:59"),
                           cand("第5回レポート", "10月27日", "2026-10-27")]
        plan, results = self.run_sync()
        cs = self.candidates()
        self.assertEqual(sorted(c["data"]["title"] for c in cs), ["第4回レポート", "第5回レポート"])
        self.assertEqual({c["data"]["deadline_candidates"][0]["precision"] for c in cs}, {"datetime", "date"})
        note = (self.env.vault / "Inbox/大学_自動取込/メール候補.md").read_text(encoding="utf-8")
        self.assertIn("第4回レポート", note)
        self.assertNotIn("- [ ]", note)
        self.assertEqual(len([l for l in self.env.task_lines() if "レポート" in l and "第4回" in l]), 0)  # 自動登録しない

    def test_fabricated_deadline_dropped(self):
        self.gmail.add(message("m3", "prof@example.ac.jp", "次回の課題", "次回までに演習問題を解いておくこと。"))
        self.llm.result = [cand("演習問題", "10月15日", "2026-10-15", evidence="次回までに演習問題を解いておくこと。")]
        self.run_sync()
        c = self.candidates()[0]["data"]
        self.assertEqual(c["deadline_candidates"], [])
        self.assertIn("本文に見つからない", c["review_reason"])
        self.assertTrue(c["needs_review"])

    def test_llm_failure_retried_and_classroom_continues(self):
        self.gmail.add(message("m4", "prof@example.ac.jp", "連絡", "第6回は10月30日締切"))
        plan, results = self.run_sync(llm=FakeLLM(fail=True))
        self.assertEqual(len(self.env.task_lines()), 4)  # Classroom の課題は登録済み
        self.assertEqual(results["status"], "ok")
        self.assertEqual(plan.mail.retry_waiting, 1)
        self.llm.result = [cand("第6回", "10月30日", "2026-10-30")]
        plan, _ = self.run_sync()
        self.assertEqual(self.llm.calls, 1)
        self.assertEqual(len(self.candidates()), 1)

    def test_llm_gives_up_after_three_attempts(self):
        self.gmail.add(message("m5", "prof@example.ac.jp", "連絡", "本文"))
        bad = FakeLLM(fail=True)
        for _ in range(4):
            self.run_sync(llm=bad)
        self.assertEqual(bad.calls, 3)
        s = self.store()
        try:
            self.assertTrue(any(r["kind"] == "メールの解析に失敗" for r in s.open_reviews()))
        finally:
            s.close()

    def test_same_message_not_reprocessed_but_new_message_in_thread_is(self):
        self.gmail.add(message("m6", "prof@example.ac.jp", "課題", "第7回は11月1日締切", thread="T"))
        self.llm.result = lambda m: [cand("第7回", "11月1日", "2026-11-01")] if "第7回" in m.body else [cand("第8回", "11月8日", "2026-11-08")]
        self.run_sync()
        self.gmail.history_queue.append("m6")  # 同じメッセージがもう一度通知されても
        self.gmail.add(message("m7", "prof@example.ac.jp", "Re: 課題", "第8回は11月8日締切", thread="T"))
        self.run_sync()
        self.assertEqual(sorted(c["data"]["title"] for c in self.candidates()), ["第7回", "第8回"])
        self.assertEqual(self.llm.calls, 2)

    def test_ignored_sender_not_stored(self):
        self.gmail.add(message("m8", "shop@example.com", "セール", "10月20日まで"))
        self.gmail.add(message("m9", "me@gl.example.ac.jp", "送信済み", "x", labels=("SENT",)))
        self.gmail.add(message("m9b", "就職支援 <career@office.example.ac.jp>", "説明会", "10月22日開催"))
        self.run_sync()
        s = self.store()
        try:
            r = s.mail("m8")
            self.assertEqual((r["status"], r["subject"], r["sender"]), ("ignored", None, None))
            self.assertEqual(s.mail("m9")["status"], "ignored")
            self.assertEqual(s.mail("m9b")["status"], "ignored")
        finally:
            s.close()
        self.assertEqual(self.llm.calls, 0)

    def test_attachments_saved_to_storage_drive_and_retried(self):
        self.gmail.add(message("m10", "prof@example.ac.jp", "資料", "配布資料です", attachments=["a.pdf"]))
        self.llm.result = [cand("配布資料", "", "", et="info")]
        self.run_sync(drive=False)
        self.assertEqual(self.drive.uploaded, [])
        self.run_sync(drive=True)
        self.assertEqual(self.drive.uploaded, [("folder-1", "a.pdf")])
        self.assertEqual(self.drive.folder, ("未分類", "未分類", "サンプル科目A"))
        self.run_sync(drive=True)
        self.assertEqual(len(self.drive.uploaded), 1)  # 二重保存しない
        note = (self.env.vault / "Inbox/大学_自動取込/メール候補.md").read_text(encoding="utf-8")
        self.assertIn("[a.pdf](https://drive.google.com/file/d/d-a.pdf/view)", note)

    def test_history_expired_falls_back_to_range(self):
        self.gmail.add(message("m11", "prof@example.ac.jp", "課題", "第9回は11月10日締切"))
        self.llm.result = [cand("第9回", "11月10日", "2026-11-10")]
        self.run_sync()
        self.gmail.expired = True
        plan, _ = self.run_sync()
        self.assertIn("期限切れ", plan.mail.detail)
        self.assertIn("newer_than:14d", self.gmail.last_query)
        self.assertEqual(self.llm.calls, 1)  # message ID で重複を除外

    def test_dry_run_changes_nothing_and_skips_llm(self):
        self.gmail.add(message("m12", "prof@example.ac.jp", "課題", "第10回"))
        plan, results = self.run_sync(dry_run=True)
        self.assertIsNone(results)
        self.assertEqual(plan.mail.llm_planned, 1)
        self.assertEqual(self.llm.calls, 0)
        self.assertFalse(self.env.state.exists())

    def test_dry_run_on_old_schema_db(self):
        self.run_sync()
        st = StateStore.open(self.env.state)
        st.conn.executescript("DROP TABLE kv; DROP TABLE mail_messages;")
        st.close()
        before = (self.env.state / "state.sqlite3").read_bytes()
        self.gmail.add(message("m15", "prof@example.ac.jp", "課題", "x"))
        plan, _ = self.run_sync(dry_run=True)
        self.assertEqual(plan.mail.new_ids, 1)
        self.assertEqual((self.env.state / "state.sqlite3").read_bytes(), before)

    def test_accept_candidate_creates_task_and_survives_classroom_sync(self):
        self.gmail.add(message("m13", "prof@example.ac.jp", "課題", "第11回は11月12日締切です"))
        self.llm.result = [cand("第11回", "11月12日", "2026-11-12")]
        self.run_sync()
        cid = self.candidates()[0]["candidate_id"]
        note = accept_candidate(self.env.cfg, cid, due=None, course_id=None)
        self.assertTrue((self.env.vault / note).exists())
        line = next(l for l in self.env.task_lines() if "第11回" in l)
        self.assertIn("📅 2026-11-12", line)
        plan, _ = self.run_sync()
        self.assertEqual(plan.missing, [])  # メール由来の課題を「一覧から消えた」扱いしない
        self.assertEqual(self.candidates(), [])

    def test_due_conflict_with_classroom_kept_as_review(self):
        self.run_sync()
        s = self.store()
        a = next(r for r in s.all_assignments() if r["title"] == "第1回レポート")
        s.close()
        link = f"https://classroom.google.com/c/{b64('100000000001')}/sa/{b64('1')}/details"
        # 数字 ID の課題に紐付くよう source_key を合わせる
        st = StateStore.open(self.env.state)
        st.conn.execute("UPDATE assignments SET source_key=? WHERE assignment_id=?",
                        ("classroom:sample-account:100000000001:1", a["assignment_id"]))
        st.conn.commit()
        st.close()
        self.gmail.add(message("m14", "prof@example.ac.jp", "延期", f"第1回レポートは10月25日に延期します {link}"))
        self.llm.result = [cand("第1回レポート", "10月25日", "2026-10-25", et="deadline_change")]
        self.run_sync()
        s = self.store()
        try:
            self.assertTrue(any(r["kind"] == "期限の矛盾" for r in s.open_reviews()))
        finally:
            s.close()
        self.assertIn("📅 2026-10-20", next(l for l in self.env.task_lines() if "第1回レポート" in l))  # 自動では変えない


class AdapterTests(unittest.TestCase):
    def test_cli_has_no_tools(self):
        import subprocess
        from unittest import mock
        out = json.dumps({"is_error": False, "structured_output": {"candidates": []}})
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0, stdout=out, stderr="")) as r:
            ClaudeCliAdapter(claude_bin="claude").extract(parse(message("x", "a@example.ac.jp", "s", "rm -rf ~")), [])
        args = r.call_args.args[0]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("<mail>", r.call_args.kwargs["input"])

    def test_validate_rejects_unknown_course_and_bad_time(self):
        m = parse(message("x", "a@example.ac.jp", "s", "第1回は10月1日 25:99 締切"))
        c = validate([cand("第1回", "10月1日", "2026-10-01", "25:9", courses=["存在しない授業"])], m, ["科目A"])[0]
        self.assertEqual(c["course_candidates"], [])
        self.assertEqual(c["deadline_candidates"][0]["precision"], "date")


if __name__ == "__main__":
    unittest.main()
