"""university-sync CLI（設計書 12 章）。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from .collectors import CollectorError, paginate
from .collectors.sample import SampleClassroomCollector
from .config import TEST_VAULT_MARKER, Config, ConfigError, load_config, missing_settings
from .normalize import normalize_course
from .state import StateStore
from .sync import SyncBlocked, make_guard, paths, run
from .timeutil import iso_now
from .vault import GuardError

EXIT_OK, EXIT_PARTIAL, EXIT_FAILED, EXIT_BLOCKED = 0, 1, 2, 3


def make_collector(cfg: Config):
    if cfg.source_mode == "sample":
        if not cfg.sample_dir:
            raise CollectorError("source.sample_dir が未設定です", permanent=True)
        return SampleClassroomCollector(cfg.sample_dir)
    from .collectors.classroom import GoogleClassroomCollector
    return GoogleClassroomCollector(cfg)


def make_mail_factory(cfg: Config):
    """メールの段に使う部品（Gmail・Claude・Drive）を組み立てる関数を返す。未設定なら None。"""
    if cfg.source_mode != "google" or not cfg.sender_domains:
        return None

    def factory(store):
        from .collectors.gmail import GoogleGmailCollector
        from .drive import DriveStore
        from .llm import ClaudeCliAdapter

        gmail = GoogleGmailCollector(cfg)
        llm = ClaudeCliAdapter(cfg.llm_model) if cfg.llm_mode == "claude-cli" else None
        drive = None
        if cfg.storage_email:
            try:
                drive = DriveStore(cfg, store)
            except CollectorError as e:
                print(f"  Drive 未接続（添付は次回以降に保存）: {e}", file=sys.stderr)
        return gmail, llm, drive

    return factory


def make_copy_factory(cfg: Config):
    """Classroom 資料を保存先 Drive へ写す部品（学校 Drive の読み取り・保存先 Drive）。未設定なら None。"""
    if cfg.source_mode != "google" or not cfg.copy_materials or not cfg.storage_email:
        return None

    def factory(store):
        from .drive import DriveStore, SchoolDriveReader

        return SchoolDriveReader(cfg), DriveStore(cfg, store)

    return factory


def make_calendar_factory(cfg: Config):
    """締切を保存先アカウントのカレンダーへ登録する部品。カレンダー未設定なら None。"""
    if cfg.source_mode != "google" or not cfg.calendar_id or not cfg.storage_email:
        return None

    def factory(store):
        from .gcalendar import GoogleCalendar

        return GoogleCalendar(cfg)

    return factory


def cmd_doctor(cfg: Config, args) -> int:
    ok = True

    def line(mark: str, msg: str) -> None:
        print(f"  [{mark}] {msg}")

    print("■ 設定")
    line("OK", f"設定ファイル: {cfg.path}")
    line("OK" if cfg.source_mode == "sample" else "--", f"取得元: {cfg.source_mode}" + ("（サンプル。実 API には接続しない）" if cfg.source_mode == "sample" else ""))

    print("■ Vault")
    if cfg.vault_path.is_dir():
        line("OK", f"Vault: {cfg.vault_path}" + ("（テスト用）" if cfg.is_test_vault else ""))
    else:
        ok = False
        line("NG", f"Vault がありません: {cfg.vault_path}")
    for rule in [Path("~/.claude/CLAUDE.md").expanduser(), cfg.vault_path / "CLAUDE.md"]:
        line("OK" if rule.exists() else "!!", f"ルールソース: {rule}")
    guard = make_guard(cfg)
    for rel in [cfg.tasks_note, paths(cfg)["status_note"], paths(cfg)["review_note"]]:
        try:
            guard.check(rel)
            line("OK", f"書込許可: {rel}")
        except GuardError as e:
            ok = False
            line("NG", str(e))
    for probe in ["Note/probe.md", "タスク置き場.md", "Dashboard.md", "../outside.md"]:
        try:
            guard.check(probe)
            ok = False
            line("NG", f"拒否されるべき書込先が通った: {probe}")
        except GuardError:
            line("OK", f"書込拒否を確認: {probe}")
    hook = cfg.vault_path / ".claude/hooks/protect-note.sh"
    line("OK" if hook.exists() else "--", f"Note/ 保護フック: {'あり' if hook.exists() else 'なし'}"
         "（Vault を作業ディレクトリにした Claude Code にだけ効く。本 CLI は自前で Note/ を拒否する）")

    print("■ 状態ストア")
    line("OK" if cfg.state_dir.exists() else "--", f"{cfg.state_dir}" + ("" if cfg.state_dir.exists() else "（初回 run で作成）"))

    print("■ Google クライアント（Phase 1 以降）")
    for mod in ["googleapiclient", "google_auth_oauthlib", "keyring"]:
        line("OK" if importlib.util.find_spec(mod) else "--", f"{mod}: {'導入済み' if importlib.util.find_spec(mod) else '未導入'}")

    miss = missing_settings(cfg)
    print(f"■ 未確定の設定（{len(miss)} 件）")
    for m in miss:
        line("..", f"{m.key} — {m.label}［{m.when} / {m.blocks}］")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_courses(cfg: Config, args) -> int:
    collector = make_collector(cfg)
    mapped = {c.course_id: c for c in cfg.courses}
    for c in map(normalize_course, paginate(collector.courses_page)):
        m = mapped.get(c.course_id)
        tag = f"→ {m.vault_dir or '（保存先未設定）'}" if m else "（対象外）"
        print(f"{c.course_id}\t{c.name}\t{c.section}\t{c.state}\t{tag}")
    return EXIT_OK


def cmd_run(cfg: Config, args) -> int:
    from .sync import last_success_age_hours, record_blocked

    if args.if_stale is not None and not args.dry_run:
        age = last_success_age_hours(cfg)
        if age is not None and age < args.if_stale:
            print(f"{iso_now()} 直近 {age:.1f} 時間以内に同期済みのため実行しない（--if-stale {args.if_stale}）")
            return EXIT_OK
    try:
        collector = make_collector(cfg)
    except (CollectorError, ConfigError) as e:
        if not args.dry_run:
            record_blocked(cfg, str(e))
        raise
    plan, results = run(cfg, collector, dry_run=args.dry_run, mail_factory=make_mail_factory(cfg),
                        copy_factory=make_copy_factory(cfg), calendar_factory=make_calendar_factory(cfg))
    label = "dry-run（変更計画のみ。Vault・状態・Google には書き込まない）" if args.dry_run else "同期"
    print(f"■ {label}  {plan.started_at}")
    c = plan.copy
    if c is not None:
        if args.dry_run:
            print(f"  [{'OK' if c.ok or c.planned else 'NG'}] 資料コピー: 写す予定 {c.planned} 件・コピー不可 {len(c.restricted)} 件"
                  + (f"（{c.detail}）" if c.detail else ""))
        else:
            print(f"  [{'OK' if c.ok else 'NG'}] 資料コピー: 新しく写した {c.copied} 件・コピー不可 {len(c.restricted)} 件"
                  + (f"（{c.detail}）" if c.detail else ""))
        for e in c.errors[:5]:
            print(f"      {e}")
    m = plan.mail
    if m is None:
        print(f"  取得元: {cfg.source_mode} / メール: 未設定")
    elif not m.ok and not m.new_ids:
        print(f"  [NG] メール: {m.detail}")
    elif args.dry_run:
        print(f"  [OK] メール: 新着 {m.new_ids} 件・対象 {m.relevant} 件・Claude に渡す予定 {m.llm_planned} 件（dry-run では呼ばない）")
    else:
        print(f"  [{'OK' if m.ok else 'NG'}] メール: 新着 {m.new_ids} 件・対象 {m.relevant} 件・Claude 呼び出し {m.llm_calls} 回・"
              f"新しい候補 {len(m.candidates)} 件・Classroom 通知の紐付け {m.classroom_linked} 件・添付保存 {m.attachments_saved} 件"
              + (f"・再試行待ち {m.retry_waiting} 件" if m.retry_waiting else "") + (f"（{m.detail}）" if m.detail else ""))
        for e in m.attachment_errors:
            print(f"      添付の保存失敗: {e}")
    k = plan.calendar
    if k is not None:
        if args.dry_run:
            p = k.planned
            print(f"  [{'OK' if k.ok else 'NG'}] 締切カレンダー: 作成予定 {p.get('create', 0)} 件・更新予定 {p.get('update', 0)} 件・"
                  f"削除予定 {p.get('remove', 0)} 件（今回取得分の適用前の状態で数えた）" + (f"（{k.detail}）" if k.detail else ""))
        else:
            print(f"  [{'OK' if k.ok else 'NG'}] 締切カレンダー: 作成 {k.created} 件・更新 {k.updated} 件・削除 {k.removed} 件"
                  + (f"（{k.detail}）" if k.detail else ""))
        for e in k.errors[:5]:
            print(f"      {e}")
    for s, ok, d in plan.sources:
        print(f"  [{'OK' if ok else 'NG'}] {s}: {d}")
    if plan.unmapped_courses:
        print(f"  対象外の授業 {len(plan.unmapped_courses)} 件（courses で確認）")
    print(f"■ 変更 {len(plan.ops)} 件" + ("（初回取り込み）" if plan.initial else ""))
    for op in plan.ops:
        if op["op"] == "resources":
            print(f"  - 資料一覧: 資料 {op['count']} 件・お知らせ {op['announcements']} 件")
            print(f"      ノート: {op['note_path']}")
            continue
        a = op["assignment"]
        print(f"  - {','.join(op['changes'])}: {a['title']}  締切={a['due_at'] or '未設定'}  提出={a['submission_state_raw']}")
        print(f"      ノート: {op['note_path']}")
    if plan.missing:
        print(f"■ 一覧から消えた {len(plan.missing)} 件（削除しない・要確認へ）")
        for aid, t in plan.missing:
            print(f"  - {aid} {t}")
    if plan.reviews:
        print(f"■ 要確認 {len(plan.reviews)} 件")
        for rid, kind, _, d in plan.reviews:
            print(f"  - [{kind}] {d}")
    notes = results["notifications"] if results else [m for _, _, _, m in plan.notifications]
    if notes:
        print(f"■ 通知候補 {len(notes)} 件（Phase 4 まで外部送信はしない）")
        for n in notes:
            print(f"  - {n}")
    if results is None:
        return EXIT_FAILED if plan.fetch_failed else EXIT_OK
    print(f"■ 結果: {results['status']}  適用 {results['done']} / 保留 {results['skipped']}")
    return {"ok": EXIT_OK, "partial": EXIT_PARTIAL}.get(results["status"], EXIT_FAILED)


def cmd_status(cfg: Config, args) -> int:
    store = StateStore.open(cfg.state_dir, read_only=True)
    try:
        print("■ 情報源")
        for s in store.source_statuses():
            print(f"  {s['source']}: {s['status']}  最終成功={s['last_success_at'] or '—'}  {s['detail'] or ''}")
        print("■ 直近の実行")
        for r in store.last_runs():
            print(f"  {r['started_at']} {r['mode']} {r['status']} {r['summary'] or ''}")
        print(f"■ 書込待ち {len(store.pending_ops())} 件 / 要確認 {len(store.open_reviews())} 件 / 管理中の課題 {len(store.all_assignments())} 件")
    finally:
        store.close()
    return EXIT_OK


def cmd_review(cfg: Config, args) -> int:
    from .sync import accept_candidate, dismiss_candidate

    if args.accept:
        note = accept_candidate(cfg, args.accept, due=args.due, course_id=args.course)
        print(f"課題として登録しました: {note}")
        return EXIT_OK
    if args.dismiss:
        ok = dismiss_candidate(cfg, args.dismiss)
        print("不要として閉じました" if ok else "該当する未確認の候補がありません")
        return EXIT_OK if ok else EXIT_FAILED
    if args.resolve:
        store = StateStore.open(cfg.state_dir)
        try:
            ok = store.resolve_review(args.resolve, iso_now())
        finally:
            store.close()
        print("解決済みにしました" if ok else "該当する未解決の項目がありません")
        return EXIT_OK if ok else EXIT_FAILED
    store = StateStore.open(cfg.state_dir, read_only=True)
    try:
        rows = store.open_reviews()
        for r in rows:
            print(f"{r['review_id']}\t{r['kind']}\t{r['detail']}")
        cands = store.open_candidates()
        for c in cands:
            d = json.loads(c["data"])
            dl = " / ".join(f"{x['date']} {x['time']}".strip() for x in d["deadline_candidates"]) or "期限なし"
            print(f"{c['candidate_id']}\tメール候補\t{d['title']}（{'、'.join(d['course_candidates']) or '授業不明'}・{dl}）")
        if not rows and not cands:
            print("要確認の項目はありません")
    finally:
        store.close()
    return EXIT_OK


def cmd_auth(cfg: Config, args) -> int:
    from . import auth
    from .collectors.classroom import SCOPES as CLASSROOM_SCOPES
    from .collectors.gmail import SCOPES as GMAIL_SCOPES
    from .drive import SCOPES as DRIVE_SCOPES

    if args.forget:
        print("キーチェーンから削除しました" if auth.forget(cfg, args.role) else "保存された資格情報はありません")
        return EXIT_OK
    from .drive import READ_SCOPES as DRIVE_READ_SCOPES
    from .gcalendar import SCOPES as CALENDAR_SCOPES

    if args.role == "school":
        scopes = CLASSROOM_SCOPES + (GMAIL_SCOPES if cfg.sender_domains else []) + (DRIVE_READ_SCOPES if cfg.copy_materials else [])
    else:
        scopes = DRIVE_SCOPES + (CALENDAR_SCOPES if cfg.calendar_id else [])
    info = auth.authorize(cfg, args.role, scopes)
    print(f"認可済み: {info['email']}（account ID {info['account_id']}）")
    print("許可された権限:")
    for s in info["scopes"]:
        print(f"  - {s}")
    print("資格情報は macOS キーチェーン（university-sync）に保存した")
    return EXIT_OK


def cmd_schedule(cfg: Config, args) -> int:
    from . import schedule

    if args.action == "install":
        t = args.time or cfg.daily_time
        if not t:
            print("時刻が未設定です（--time HH:MM または schedule.daily_time）", file=sys.stderr)
            return EXIT_BLOCKED
        print(f"登録しました: {schedule.install(cfg, t)}")
        print(schedule.status())
    elif args.action == "uninstall":
        print("登録を解除しました" if schedule.uninstall() else "登録されていません")
    elif args.action == "run-now":
        schedule.kickstart()
        print("定期実行のジョブを今すぐ起動しました（結果はログと同期状況ノート）")
    else:
        print(schedule.status())
    return EXIT_OK


def cmd_init_test_vault(args) -> int:
    """本番 Vault の大学タスク運用を模したテスト用 Vault を作る（本番は読まない・変えない）。"""
    root = Path(args.dir).expanduser().resolve()
    if root.exists() and any(root.iterdir()) and not (root / TEST_VAULT_MARKER).exists():
        print(f"空でない既存ディレクトリには作らない: {root}", file=sys.stderr)
        return EXIT_FAILED
    if (root / TEST_VAULT_MARKER).exists():
        shutil.rmtree(root)
    (root / "Areas/タスク管理").mkdir(parents=True)
    (root / "Areas/University/サンプル科目A").mkdir(parents=True)
    (root / "Note").mkdir()
    (root / "Note/本人メモ.md").write_text("本人専用\n", encoding="utf-8")
    (root / "タスク置き場.md").write_text("- [ ] 自由に書いたタスク\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# テスト用 Vault\n", encoding="utf-8")
    (root / "Areas/タスク管理/大学.md").write_text(
        "---\ntags: [area, university]\n---\n\n# 🎓 大学\n\n## タスク\n"
        "- [x] 過去の本人タスク #university 📅 2026-07-24 ✅ 2026-07-24\n\n## メモ\n\n- \n",
        encoding="utf-8")
    (root / TEST_VAULT_MARKER).write_text("university-sync のテスト用 Vault\n", encoding="utf-8")
    print(f"テスト用 Vault を作成: {root}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="university-sync")
    p.add_argument("--config", type=Path, help="設定ファイル（既定: ~/.config/university-sync/config.toml）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="接続・設定・書込先の検証")
    sub.add_parser("courses", help="閲覧可能な授業一覧")
    r = sub.add_parser("run", help="同期")
    r.add_argument("--dry-run", action="store_true", help="変更計画だけ表示")
    r.add_argument("--if-stale", type=float, metavar="時間", help="最後の成功からこの時間が経っていなければ何もしない（定期実行用）")
    sub.add_parser("status", help="最終成功・待ち処理・エラー")
    rv = sub.add_parser("review", help="競合・曖昧な候補の確認")
    rv.add_argument("--resolve", metavar="ID")
    rv.add_argument("--accept", metavar="候補ID", help="メール候補を課題として登録")
    rv.add_argument("--dismiss", metavar="候補ID", help="メール候補を不要として閉じる")
    rv.add_argument("--due", help="登録時の期限 YYYY-MM-DD または YYYY-MM-DDTHH:MM（JST）")
    rv.add_argument("--course", help="登録先の授業 course_id")
    sc = sub.add_parser("schedule", help="毎日の定期実行（launchd）の登録・解除・確認")
    sc.add_argument("action", choices=["install", "uninstall", "status", "run-now"])
    sc.add_argument("--time", help="実行時刻 HH:MM（既定は schedule.daily_time）")
    a = sub.add_parser("auth", help="Google アカウントの認可（ブラウザが開く）")
    a.add_argument("role", choices=["school", "storage"])
    a.add_argument("--forget", action="store_true", help="キーチェーンの資格情報を削除")
    t = sub.add_parser("init-test-vault", help="検証用の空 Vault を作る")
    t.add_argument("dir")
    args = p.parse_args(argv)

    if args.cmd == "init-test-vault":
        return cmd_init_test_vault(args)
    try:
        cfg = load_config(args.config)
        handler = {"schedule": cmd_schedule, "auth": cmd_auth, "doctor": cmd_doctor, "courses": cmd_courses, "run": cmd_run,
                   "status": cmd_status, "review": cmd_review}[args.cmd]
        return handler(cfg, args)
    except (ConfigError, SyncBlocked) as e:
        print(f"停止: {e}", file=sys.stderr)
        return EXIT_BLOCKED
    except CollectorError as e:
        print(f"取得元エラー: {e}", file=sys.stderr)
        return EXIT_BLOCKED if e.permanent else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
