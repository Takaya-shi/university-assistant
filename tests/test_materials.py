"""Classroom 資料の保存先 Drive へのコピー（Phase 5）。実際の Drive には触れない。"""

from __future__ import annotations

import unittest

from test_sync import Env

from university_sync.collectors import CollectorError
from university_sync.collectors.sample import SampleClassroomCollector
from university_sync.sync import run

EXTRA = """
[accounts.storage]
email = "me@gmail.example"
copy_classroom_materials = true
"""


class FakeReader:
    def __init__(self):
        self.files = {
            "F1": {"id": "F1", "name": "slide01.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-10-01T00:00:00Z",
                   "size": "10", "capabilities": {"canDownload": True}},
            "F2": {"id": "F2", "name": "小テスト第1回.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-10-06T00:00:00Z",
                   "size": "10", "capabilities": {"canDownload": False}},
            "SAMPLE_FILE_1": {"id": "SAMPLE_FILE_1", "name": "講義資料第1回", "mimeType": "application/vnd.google-apps.document",
                              "modifiedTime": "2026-10-01T00:00:00Z", "capabilities": {"canDownload": True}},
        }
        self.fail = set()

    def meta(self, fid):
        if fid in self.fail:
            raise CollectorError("HTTP 500", status=500)
        return dict(self.files[fid])

    def content(self, meta):
        from university_sync.drive import EXPORTS, NATIVE_TARGET
        if meta["mimeType"] in EXPORTS:
            return b"docx", EXPORTS[meta["mimeType"]], NATIVE_TARGET.get(meta["mimeType"])
        return b"%PDF", meta["mimeType"], None


class FakeStorage:
    def __init__(self):
        self.uploads = []

    def folder_for(self, year, term, course, sub="メール添付"):
        self.folder = (year, term, course, sub)
        return "fold"

    def upload(self, folder, name, mime, data, desc, target_mime=None):
        self.uploads.append((name, mime, target_mime))
        n = len(self.uploads)
        return {"id": f"c{n}", "webViewLink": f"https://drive.google.com/file/d/c{n}/view"}


class MaterialCopyTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.use("sample_v1", EXTRA)
        self.reader, self.drive = FakeReader(), FakeStorage()

    def tearDown(self):
        self.env.cleanup()

    def sync(self, dry_run=False):
        return run(self.env.cfg, SampleClassroomCollector(self.env.cfg.sample_dir), dry_run=dry_run,
                   copy_factory=lambda store: (self.reader, self.drive))

    def resources(self):
        return (self.env.vault / self.env.vault_dir / "資料一覧.md").read_text(encoding="utf-8")

    def test_copies_downloadable_and_marks_restricted(self):
        plan, _ = self.sync()
        self.assertEqual(sorted(u[0] for u in self.drive.uploads), ["slide01.pdf", "講義資料第1回"])
        self.assertEqual(plan.copy.restricted, ["小テスト第1回.pdf"])
        text = self.resources()
        self.assertIn("| 保存コピー |", text)
        self.assertIn("コピー不可（制限）", text)
        self.assertEqual(text.count("[コピー](https://drive.google.com/file/d/c"), 2)
        self.assertEqual(self.drive.folder[3], "授業資料")

    def test_google_doc_kept_native(self):
        self.sync()
        doc = next(u for u in self.drive.uploads if u[0] == "講義資料第1回")
        self.assertEqual(doc[2], "application/vnd.google-apps.document")  # PDF にせず元の形式へ戻す

    def test_unchanged_not_recopied_and_updated_recopied(self):
        self.sync()
        self.sync()
        self.assertEqual(len(self.drive.uploads), 2)
        self.reader.files["F1"]["modifiedTime"] = "2026-10-09T00:00:00Z"
        self.sync()
        self.assertEqual(len(self.drive.uploads), 3)
        self.assertIn("file/d/c3/view", self.resources())

    def test_failure_retried(self):
        self.reader.fail = {"F1"}
        plan, results = self.sync()
        self.assertFalse(plan.copy.ok)
        self.assertIn("未保存（再試行待ち）", self.resources())
        self.reader.fail = set()
        self.sync()
        self.assertIn("slide01.pdf", [u[0] for u in self.drive.uploads])

    def test_dry_run_copies_nothing(self):
        plan, _ = self.sync(dry_run=True)
        self.assertEqual(plan.copy.planned, 2)
        self.assertEqual(self.drive.uploads, [])
        self.assertFalse(self.env.state.exists())


if __name__ == "__main__":
    unittest.main()
