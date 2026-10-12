"""設計書 13 章「重点検証ケース」をサンプルデータとテスト用 Vault で確かめる。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from university_sync import sync as sync_mod
from university_sync.cli import cmd_init_test_vault, main
from university_sync.collectors.sample import SampleClassroomCollector
from university_sync.config import load_config, missing_settings
from university_sync.state import StateStore
from university_sync.sync import SyncBlocked, run
from university_sync.timeutil import classroom_due_to_jst
from university_sync.vault import GuardError, VaultGuard, find_task, update_task_line

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TASKS = "Areas/タスク管理/大学.md"


class Env:
    def __init__(self, scenario: str = "sample_v1", vault_dir: str = "Areas/University/サンプル科目A"):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.state = self.tmp / "state"
        self.fixtures = self.tmp / "fixtures"
        shutil.copytree(FIXTURES, self.fixtures)
        cmd_init_test_vault(type("A", (), {"dir": str(self.vault)})())
        self.cfg_path = self.tmp / "config.toml"
        self.vault_dir = vault_dir
        self.use(scenario)

    def use(self, scenario: str, extra: str = "") -> None:
        self.scenario = self.fixtures / scenario
        self.cfg_path.write_text(f"""
[vault]
path = "{self.vault}"
[state]
dir = "{self.state}"
[source]
mode = "sample"
sample_dir = "{self.scenario}"
[[courses]]
course_id = "100000000001"
name = "サンプル科目A"
vault_dir = "{self.vault_dir}"
{extra}
""", encoding="utf-8")
        self.cfg = load_config(self.cfg_path)

    def run(self, dry_run: bool = False):
        return run(self.cfg, SampleClassroomCollector(self.cfg.sample_dir), dry_run=dry_run)

    def tasks(self) -> str:
        return (self.vault / TASKS).read_text(encoding="utf-8")

    def task_lines(self) -> list[str]:
        return [l for l in self.tasks().split("\n") if "university-sync:id=" in l]

    def notes(self) -> list[Path]:
        return sorted((self.vault / self.vault_dir / "課題").glob("*.md"))

    def snapshot(self) -> dict[str, str]:
        return {str(p.relative_to(self.vault)): p.read_text(encoding="utf-8")
                for p in self.vault.rglob("*") if p.is_file()}

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp)


class TimeTests(unittest.TestCase):
    def test_utc_to_jst_crosses_date(self):
        self.assertEqual(classroom_due_to_jst({"year": 2026, "month": 10, "day": 21}, {"hours": 15, "minutes": 30}),
                         ("2026-10-22T00:30+09:00", "datetime"))

    def test_omitted_minutes_are_zero(self):
        self.assertEqual(classroom_due_to_jst({"year": 2026, "month": 10, "day": 15}, {"hours": 3})[0], "2026-10-15T12:00+09:00")

    def test_no_due_and_date_only(self):
        self.assertEqual(classroom_due_to_jst(None, None), (None, "unknown"))
        self.assertEqual(classroom_due_to_jst({"year": 2026, "month": 10, "day": 15}, None), ("2026-10-15", "date"))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.g = VaultGuard(self.tmp, [TASKS, "Areas/University/"], ["Note/", "タスク置き場.md"])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_rejects(self):
        for rel in ["Note/a.md", "タスク置き場.md", "Dashboard.md", "../x.md", "Areas/University/../../Note/a.md"]:
            with self.assertRaises(GuardError, msg=rel):
                self.g.check(rel)

    def test_symlink_into_note_rejected(self):
        (self.tmp / "Note").mkdir()
        (self.tmp / "Areas/University").mkdir(parents=True)
        (self.tmp / "Areas/University/link").symlink_to(self.tmp / "Note")
        with self.assertRaises(GuardError):
            self.g.check("Areas/University/link/a.md")

    def test_allows(self):
        self.g.check(TASKS)
        self.g.check("Areas/University/科目/課題/x.md")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_dry_run_writes_nothing(self):
        before = self.env.snapshot()
        plan, results = self.env.run(dry_run=True)
        self.assertIsNone(results)
        self.assertEqual(len(plan.ops), 5)  # 課題 4 件 + 資料一覧 1 件
        self.assertEqual(self.env.snapshot(), before)
        self.assertFalse(self.env.state.exists())

    def test_all_pages_and_idempotent(self):
        self.env.run()
        self.assertEqual(len(self.env.task_lines()), 4)  # 2 ページ目の課題も取得
        first = self.env.snapshot()
        plan, results = self.env.run()
        self.assertEqual(plan.ops, [])
        self.assertEqual(len(self.env.task_lines()), 4)
        self.assertEqual(len(self.env.notes()), 4)
        changed = [k for k in first if first[k] != self.env.snapshot().get(k) and "同期状況" not in k]
        self.assertEqual(changed, [])

    def test_backup_keeps_pre_run_content(self):
        original = self.env.tasks()
        self.env.run()
        backups = list((self.env.state / "backups").rglob("大学.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def _resources(self) -> Path:
        return self.env.vault / self.env.vault_dir / "資料一覧.md"

    def test_resource_list_collects_all_sources(self):
        self.env.run()
        text = self._resources().read_text(encoding="utf-8")
        self.assertIn("[slide01.pdf](https://drive.google.com/file/d/F1/view) | 資料：第1回 講義スライド", text)
        self.assertIn("[小テスト第1回.pdf](https://drive.google.com/file/d/F2/view) | お知らせ", text)
        self.assertIn("| 課題：第1回レポート", text)
        self.assertIn("2026-10-03 | [参考［1］](https://example.com/ref) | 資料：参考 ｜ 文献", text)  # UTC→JST の日付・表の区切り
        self.assertEqual(text.count("<!-- university-sync:end -->"), 1)  # お知らせ本文のマーカーは無効化
        self.assertIn("…（続きは Classroom）", text)
        self.assertNotIn("- [ ]", text)  # 資料はタスクにしない

    def test_resource_list_idempotent_and_conflict(self):
        self.env.run()
        plan, _ = self.env.run()
        self.assertEqual([o for o in plan.ops if o["op"] == "resources"], [])
        edited = self._resources().read_text(encoding="utf-8").replace("## お知らせ（新しい順）", "## お知らせ（自分で編集）")
        self._resources().write_text(edited, encoding="utf-8")
        plan, _ = self.env.run()
        self.assertEqual(self._resources().read_text(encoding="utf-8"), edited)
        self.assertTrue(any(k == "資料一覧の自動生成部分を本人が編集" for _, k, _, _ in plan.reviews))

    def test_user_memo_outside_block_kept(self):
        self.env.run()
        text = self._resources().read_text(encoding="utf-8").replace("## 自分のメモ\n", "## 自分のメモ\n印刷済み\n")
        self._resources().write_text(text, encoding="utf-8")
        new_ann = json.loads((self.env.scenario / "announcements/100000000001.json").read_text())
        new_ann[0]["announcements"].insert(0, {"id": "a3", "text": "新しいお知らせ", "creationTime": "2026-10-10T00:00:00Z"})
        (self.env.scenario / "announcements/100000000001.json").write_text(json.dumps(new_ann, ensure_ascii=False))
        self.env.run()
        out = self._resources().read_text(encoding="utf-8")
        self.assertIn("印刷済み", out)
        self.assertIn("新しいお知らせ", out)

    def test_materials_failure_keeps_list(self):
        self.env.run()
        before = self._resources().read_text(encoding="utf-8")
        (self.env.scenario / "failures.json").write_text(json.dumps({"announcements": ["100000000001"]}))
        (self.env.scenario / "materials/100000000001.json").write_text("[]")
        plan, results = self.env.run()
        self.assertEqual(self._resources().read_text(encoding="utf-8"), before)
        self.assertEqual(results["status"], "partial")

    def test_existing_user_tasks_preserved(self):
        self.env.run()
        self.assertIn("- [x] 過去の本人タスク #university 📅 2026-07-24 ✅ 2026-07-24", self.env.tasks())
        self.assertEqual((self.env.vault / "タスク置き場.md").read_text(encoding="utf-8"), "- [ ] 自由に書いたタスク\n")
        self.assertEqual((self.env.vault / "Note/本人メモ.md").read_text(encoding="utf-8"), "本人専用\n")

    def test_due_change_updates_same_task_and_keeps_user_fields(self):
        self.env.run()
        t = self.env.tasks()
        line = next(l for l in self.env.task_lines() if "第1回レポート" in l)
        (self.env.vault / TASKS).write_text(t.replace(line, line + " ⏳ 2026-10-12 ⏫"), encoding="utf-8")
        note = next(p for p in self.env.notes() if "第1回" in p.name)
        note.write_text(note.read_text(encoding="utf-8").replace("## 自分の回答\n", "## 自分の回答\n本人の回答\n"), encoding="utf-8")

        self.env.use("sample_v2")
        self.env.run()
        lines = [l for l in self.env.task_lines() if "第1回レポート" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("📅 2026-10-27", lines[0])
        self.assertNotIn("📅 2026-10-20", lines[0])
        self.assertIn("⏳ 2026-10-12", lines[0])
        self.assertIn("⏫", lines[0])
        self.assertTrue(note.exists())  # 期限変更でノートは移動しない
        self.assertIn("本人の回答", note.read_text(encoding="utf-8"))
        self.assertIn("2026-10-27 23:59", note.read_text(encoding="utf-8"))

    def test_submission_only_change(self):
        self.env.run()
        self.env.use("sample_v2")
        plan, _ = self.env.run(dry_run=True)
        op = next(o for o in plan.ops if o["assignment"]["title"].startswith("第2回"))
        self.assertEqual(op["changes"], ["submission"])
        self.env.run()
        line = next(l for l in self.env.task_lines() if "第2回" in l)
        self.assertTrue(line.startswith("- [x]"))

    def test_reclaimed_reopens_task(self):
        self.env.run()
        self.assertTrue(next(l for l in self.env.task_lines() if "小テスト" in l).startswith("- [x]"))
        self.env.use("sample_v2")
        _, results = self.env.run()
        line = next(l for l in self.env.task_lines() if "小テスト" in l)
        self.assertTrue(line.startswith("- [ ]"))
        self.assertNotIn("✅", line)
        self.assertTrue(any("提出取り消し" in n for n in results["notifications"]))

    def test_missing_item_not_deleted(self):
        self.env.run()
        self.env.use("sample_v2")
        plan, _ = self.env.run()
        self.assertEqual([t for _, t in plan.missing], ["期限なし課題"])
        self.assertTrue(any("期限なし課題" in l for l in self.env.task_lines()))
        self.assertTrue(any("期限なし課題" in p.name for p in self.env.notes()))
        review = (self.env.vault / "Inbox/大学_自動取込/要確認一覧.md").read_text(encoding="utf-8")
        self.assertIn("一覧から消えた", review)
        self.assertNotIn("- [ ]", review)  # 要確認一覧に Dashboard 用チェックボックスを置かない

    def test_fetch_failure_keeps_everything(self):
        self.env.run()
        before = self.env.tasks()
        (self.env.scenario / "failures.json").write_text(json.dumps({"coursework": ["100000000001"]}))
        plan, results = self.env.run()
        self.assertEqual(plan.ops, [])
        self.assertEqual(plan.missing, [])
        self.assertEqual(self.env.tasks(), before)
        self.assertEqual(results["status"], "partial")
        status = (self.env.vault / "Areas/University/連携管理/同期状況.md").read_text(encoding="utf-8")
        self.assertIn("取得できず（不明）", status)

    def test_submission_failure_keeps_previous_state(self):
        self.env.run()
        (self.env.scenario / "failures.json").write_text(json.dumps({"submissions": ["100000000001"]}))
        plan, _ = self.env.run()
        self.assertEqual(plan.ops, [])
        self.assertEqual(plan.missing, [])

    def test_user_edit_of_managed_block_is_conflict(self):
        self.env.run()
        note = next(p for p in self.env.notes() if "第1回" in p.name)
        edited = note.read_text(encoding="utf-8").replace("- 科目: サンプル科目A", "- 科目: サンプル科目A（本人メモ）")
        note.write_text(edited, encoding="utf-8")
        self.env.use("sample_v2")
        plan, _ = self.env.run()
        self.assertEqual(note.read_text(encoding="utf-8"), edited)
        self.assertTrue(any(k == "管理ブロックを本人が編集" for _, k, _, _ in plan.reviews))
        # タスク行の期限は独立して更新される
        self.assertIn("📅 2026-10-27", next(l for l in self.env.task_lines() if "第1回" in l))

    def test_crash_after_note_write_recovers_without_duplicates(self):
        real = sync_mod.VaultWriter.write
        calls = {"n": 0}

        def flaky(self, rel, content, expected):
            calls["n"] += 1
            if rel == TASKS and calls["n"] == 4:  # 2 件目の課題のタスク行書込で停止
                raise KeyboardInterrupt
            return real(self, rel, content, expected)

        with mock.patch.object(sync_mod.VaultWriter, "write", flaky):
            with self.assertRaises(KeyboardInterrupt):
                self.env.run()
        self.assertEqual(len(self.env.task_lines()), 1)
        self.assertEqual(len(self.env.notes()), 2)
        self.env.run()
        self.assertEqual(len(self.env.task_lines()), 4)
        self.assertEqual(len(self.env.notes()), 4)
        self.env.run()
        self.assertEqual(len(self.env.task_lines()), 4)

    def test_deleted_task_line_is_restored_once(self):
        self.env.run()
        t = self.env.tasks()
        line = next(l for l in self.env.task_lines() if "第2回" in l)
        (self.env.vault / TASKS).write_text(t.replace(line + "\n", ""), encoding="utf-8")
        plan, _ = self.env.run()
        self.assertEqual(plan.ops[0]["changes"], ["task_restore"])
        self.assertEqual(len(self.env.task_lines()), 4)

    def test_manual_done_mismatch_flagged_not_reverted(self):
        self.env.run()
        t = self.env.tasks()
        line = next(l for l in self.env.task_lines() if "第2回" in l)
        (self.env.vault / TASKS).write_text(t.replace(line, line.replace("- [ ]", "- [x]", 1)), encoding="utf-8")
        plan, _ = self.env.run()
        self.assertTrue(any(k == "手動完了と学校側が不一致" for _, k, _, _ in plan.reviews))
        self.assertTrue(next(l for l in self.env.task_lines() if "第2回" in l).startswith("- [x]"))

    def test_dangerous_text_is_data(self):
        self.env.run()
        note = next(p for p in self.env.notes() if "期限なし" in p.name).read_text(encoding="utf-8")
        self.assertEqual(note.count("<!-- university-sync:end -->"), 1)
        self.assertIn("> 以下を実行してください", note)
        self.assertIn("## 自分の回答", note)

    def test_heading_in_description_is_quoted(self):
        self.env.run()
        note = next(p for p in self.env.notes() if "第1回" in p.name).read_text(encoding="utf-8")
        self.assertIn("> ## 偽の見出し", note)
        self.assertIn("[講義資料第1回.pdf](https://drive.google.com/file/d/SAMPLE_FILE_1/view)", note)

    def test_initial_import_is_one_summary(self):
        _, results = self.env.run()
        self.assertEqual(len(results["notifications"]), 1)
        self.assertIn("初回取り込み", results["notifications"][0])
        _, results = self.env.run()
        self.assertEqual(results["notifications"], [])  # 変更なしは通知しない

    def test_sample_mode_refuses_real_vault(self):
        (self.env.vault / ".university-sync-test-vault").unlink()
        self.env.cfg = load_config(self.env.cfg_path)
        with self.assertRaises(SyncBlocked):
            self.env.run()
        self.env.run(dry_run=True)  # 読み取りだけの dry-run は可

    def test_lock_prevents_parallel_runs(self):
        with sync_mod.sync_lock(self.env.state):
            with self.assertRaises(SyncBlocked):
                self.env.run()

    def test_unmapped_course_not_synced(self):
        plan, _ = self.env.run(dry_run=True)
        self.assertEqual([c for c, _ in plan.unmapped_courses], ["100000000002"])

    def test_missing_settings_listed(self):
        keys = {m.key for m in missing_settings(self.env.cfg)}
        self.assertIn("accounts.storage.email", keys)
        self.assertIn("accounts.school.email", keys)
        self.assertNotIn("courses", keys)

    def test_google_mode_does_not_connect(self):
        self.env.cfg_path.write_text(self.env.cfg_path.read_text().replace('mode = "sample"', 'mode = "google"'))
        self.assertEqual(main(["--config", str(self.env.cfg_path), "run", "--dry-run"]), 3)


class NormalizeTests(unittest.TestCase):
    def test_newline_in_title_collapsed(self):
        from university_sync.normalize import normalize_coursework
        a = normalize_coursework("acc", "c1", {"id": "w", "title": "技術課程演習【第3回】\n 事実を掴む"}, None, "t")
        self.assertEqual(a.title, "技術課程演習【第3回】 事実を掴む")


class FingerprintTests(unittest.TestCase):
    def test_thumbnail_url_ignored(self):
        from university_sync.normalize import content_fingerprint
        def w(thumb):
            return {"id": "w", "title": "t", "materials": [{"driveFile": {"driveFile": {
                "id": "f", "title": "a.pptx", "alternateLink": "https://x", "thumbnailUrl": thumb}}}]}
        self.assertEqual(content_fingerprint(w("https://t?sig=1")), content_fingerprint(w("https://t?sig=2")))


class Phase4Tests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_if_stale_skips_recent_success(self):
        self.env.run()
        with mock.patch("university_sync.cli.make_collector") as mc:
            code = main(["--config", str(self.env.cfg_path), "run", "--if-stale", "12"])
        self.assertEqual(code, 0)
        mc.assert_not_called()

    def test_blocked_run_is_recorded_in_status_note(self):
        from university_sync.collectors import ReauthRequired
        self.env.run()
        with mock.patch("university_sync.cli.make_collector", side_effect=ReauthRequired("再認可が必要", permanent=True)):
            code = main(["--config", str(self.env.cfg_path), "run"])
        self.assertEqual(code, 3)
        status = (self.env.vault / "Areas/University/連携管理/同期状況.md").read_text(encoding="utf-8")
        self.assertIn("再認可が必要", status)
        self.assertIn("取得できず", status)

    def test_plist_is_noninteractive(self):
        from university_sync import schedule
        p = schedule.build_plist(self.env.cfg, 7, 0)
        self.assertEqual(p["StartCalendarInterval"], {"Hour": 7, "Minute": 0})
        self.assertIn("--if-stale", p["ProgramArguments"])
        self.assertIn("/opt/homebrew/bin", p["EnvironmentVariables"]["PATH"])
        self.assertTrue(p["EnvironmentVariables"]["USER"])
        self.assertTrue(p["StandardOutPath"].startswith(str(self.env.state)))
        self.assertTrue(p["ProgramArguments"][0].startswith(str(self.env.state)))  # ~/Documents の外から起動する

    def test_mail_fallback_covers_downtime(self):
        from datetime import timedelta
        from university_sync.mailsync import fallback_days
        from university_sync.timeutil import now_jst
        self.env.run()
        st = StateStore.open(self.env.state)
        st.set_source_status("gmail", True, (now_jst() - timedelta(days=20)).isoformat(timespec="seconds"))
        self.assertGreaterEqual(fallback_days(type("C", (), {"initial_range_days": 14})(), st), 21)
        st.close()


class ManualDueTests(unittest.TestCase):
    def test_manual_due_kept_when_school_has_none(self):
        line = "- [ ] 説明 <!-- university-sync:id=ua-0123456789 --> #university 📅 2026-10-17"
        self.assertIn("📅 2026-10-17", update_task_line(line, desc="新", due_date=None, status=None, done_date=None))
        self.assertIn("📅 2026-10-20", update_task_line(line, desc="新", due_date="2026-10-20", status=None, done_date=None))
        self.assertNotIn("📅 2026-10-17", update_task_line(line, desc="新", due_date="2026-10-20", status=None, done_date=None))


class TaskLineTests(unittest.TestCase):
    def test_update_keeps_unknown_fields(self):
        line = "- [ ] 説明 <!-- university-sync:id=ua-0123456789 --> #university 📅 2026-10-20 ⏳ 2026-10-18 🔼"
        out = update_task_line(line, desc="新しい説明", due_date=None, status=None, done_date=None, prev_due="2026-10-20")
        self.assertEqual(out, "- [ ] 新しい説明 <!-- university-sync:id=ua-0123456789 --> #university ⏳ 2026-10-18 🔼")
        self.assertEqual(find_task([out], "ua-0123456789"), 0)


if __name__ == "__main__":
    unittest.main()
