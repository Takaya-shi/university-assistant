"""同期の本体（設計書 11.1）。取得 → 正規化 → 照合 → 計画 → 適用。

dry-run は計画を作って返すだけで、Vault・状態ストアを変更しない。
通常実行では計画を pending_ops に保存してから適用し、各書込の成功後に状態を進める。
書込はすべて冪等（ID で探して更新）なので、途中停止後の再実行で重複しない。
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from . import resources
from .collectors import ClassroomCollector, CollectorError, paginate
from .config import Config, CourseMapping
from .models import Assignment, SourceRef
from .normalize import normalize_course, normalize_coursework
from .render import (render_block, render_note, render_review_block, render_status_block,
                     submission_label, task_desc, wrap_managed)
from .state import StateStore
from .timeutil import iso_now, now_jst
from .vault import (ConcurrentEditError, GuardError, TASK_RE, VaultGuard, VaultWriter, extract_block,
                    find_task, frontmatter_value, insert_task_line, render_task_line, replace_block,
                    safe_filename, sha, update_task_line)

DONE_STATES = ("TURNED_IN", "RETURNED")


class SyncBlocked(Exception):
    """設定・安全条件のため同期を始められない。"""


@dataclass
class Plan:
    run_mode: str
    started_at: str
    initial: bool
    ops: list[dict] = field(default_factory=list)
    reviews: list[tuple[str, str, str | None, str]] = field(default_factory=list)
    notifications: list[tuple[str, str, str | None, str]] = field(default_factory=list)
    sources: list[tuple[str, bool, str]] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    missing: list[tuple[str, str]] = field(default_factory=list)
    unmapped_courses: list[tuple[str, str]] = field(default_factory=list)
    fetch_failed: bool = False
    mail: object = None  # mailsync.MailReport（メール未設定なら None）
    copy: object = None  # materials.CopyReport（資料コピー未設定なら None）
    calendar: object = None  # gcalendar.CalendarReport（締切カレンダー未設定なら None）

    def summary(self) -> dict:
        kinds: dict[str, int] = {}
        for op in self.ops:
            for c in op["changes"]:
                kinds[c] = kinds.get(c, 0) + 1
        return {
            "ops": len(self.ops), "changes": kinds, "reviews": len(self.reviews),
            "notifications": len(self.notifications), "missing": len(self.missing),
            "failed_sources": [s for s, ok, _ in self.sources if not ok],
        }


def paths(cfg: Config) -> dict[str, str]:
    return {
        "status_note": f"{cfg.university_root}/連携管理/同期状況.md",
        "review_note": f"{cfg.review_inbox}/要確認一覧.md",
        "mail_note": f"{cfg.review_inbox}/メール候補.md",
    }


def make_guard(cfg: Config) -> VaultGuard:
    allowed = [cfg.tasks_note, f"{cfg.university_root}/", f"{cfg.review_inbox}/"]
    return VaultGuard(cfg.vault_path, allowed, cfg.protected_paths)


@contextmanager
def sync_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    f = open(state_dir / "sync.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise SyncBlocked("別の同期が実行中です（ロック取得失敗）")
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def assignment_from_dict(d: dict) -> Assignment:
    d = dict(d)
    d["source_refs"] = [SourceRef(**r) for r in d.get("source_refs", [])]
    return Assignment(**d)


# ---------------------------------------------------------------- 計画

def build_plan(cfg: Config, collector: ClassroomCollector, store: StateStore, writer: VaultWriter, mode: str) -> Plan:
    now = iso_now()
    plan = Plan(run_mode=mode, started_at=now,
                initial=not store.has_successful_run() and not store.all_assignments())

    try:
        courses = {c.course_id: c for c in map(normalize_course, paginate(collector.courses_page))}
        plan.sources.append(("classroom.courses", True, f"{len(courses)} 件"))
    except CollectorError as e:
        plan.sources.append(("classroom.courses", False, str(e)))
        plan.fetch_failed = True
        plan.notifications.append((f"fail:classroom.courses:{now[:10]}", "fetch_failed", None, f"授業一覧の取得に失敗: {e}"))
        return plan

    mapped_ids = {c.course_id for c in cfg.courses}
    plan.unmapped_courses = [(cid, c.name) for cid, c in courses.items() if cid not in mapped_ids]

    tasks_text = writer.read(cfg.tasks_note) or ""
    task_lines = tasks_text.split("\n")

    for course in cfg.courses:
        _plan_course(cfg, collector, store, writer, plan, course, courses, task_lines, now)

    _plan_manual_done_mismatch(store, plan, task_lines)
    _plan_approaching(cfg, store, plan)
    if plan.initial and plan.notifications:
        n = sum(1 for _, k, _, _ in plan.notifications if k == "new_assignment")
        others = [x for x in plan.notifications if x[1] != "new_assignment"]
        plan.notifications = others + [(f"initial_import:{now[:10]}", "initial_import", None,
                                        f"初回取り込み: 課題 {n} 件を登録予定")]
    return plan


def _plan_course(cfg, collector, store, writer, plan: Plan, course: CourseMapping, courses, task_lines, now) -> None:
    cid = course.course_id
    if cid not in courses:
        plan.reviews.append((f"course_absent:{cid}", "授業が一覧に無い", None,
                             f"設定済みの授業 {cid}（{course.name}）が授業一覧に出ていない。権限変更・授業終了・ID誤りを確認"))
        return
    if not course.vault_dir:
        plan.reviews.append((f"course_no_dir:{cid}", "保存先未設定", None, f"授業 {cid} の vault_dir が未設定のため同期しない"))
        return

    try:
        works = list(paginate(lambda t: collector.coursework_page(cid, t)))
        plan.sources.append((f"classroom.coursework:{cid}", True, f"{len(works)} 件"))
    except CollectorError as e:
        plan.sources.append((f"classroom.coursework:{cid}", False, str(e)))
        plan.notifications.append((f"fail:coursework:{cid}:{now[:10]}", "fetch_failed", None, f"{course.name} の課題取得に失敗: {e}"))
        return  # 失敗を「課題なし」扱いしない。既存の課題・締切には触れない

    subs_ok = True
    try:
        subs = {s["courseWorkId"]: s for s in paginate(lambda t: collector.my_submissions_page(cid, t))}
        plan.sources.append((f"classroom.submissions:{cid}", True, f"{len(subs)} 件"))
    except CollectorError as e:
        subs, subs_ok = {}, False
        plan.sources.append((f"classroom.submissions:{cid}", False, str(e)))

    fetched_ids = set()
    for w in works:
        a = normalize_coursework(collector.account_id, cid, w, subs.get(str(w["id"])), now)
        aid = a.assignment_id
        fetched_ids.add(aid)
        row = store.assignment(aid)
        if not subs_ok and row is not None:
            # 提出状態が取れなかったときは前回の確認結果を保持する
            a.submission_state_raw = row["submission_state"]
            a.submission_verified_at = row["submission_verified_at"]

        changes = []
        if row is None:
            changes.append("new")
            note_path = f"{course.vault_dir}/課題/{safe_filename(a.title)}_{aid[3:9]}.md"
            plan.notifications.append((f"new:{aid}", "new_assignment", aid, f"新規課題: {course.name} {a.title}（{a.due_at or '締切未設定'}）"))
        else:
            note_path = row["note_path"]  # 期限変更・改題でもノートは移動しない
            if row["content_hash"] != a.content_hash:
                changes.append("content")
            if row["due_at"] != a.due_at:
                changes.append("due")
                plan.notifications.append((f"due:{aid}:{a.due_at}", "due_changed", aid,
                                           f"期限変更: {course.name} {a.title} {row['due_at'] or '未設定'} → {a.due_at or '未設定'}"))
            if row["submission_state"] != a.submission_state_raw:
                changes.append("submission")
                st = a.submission_state_raw
                if st == "RECLAIMED_BY_STUDENT":
                    plan.notifications.append((f"reclaimed:{aid}:{now[:10]}", "reclaimed", aid, f"提出取り消し: {course.name} {a.title}"))
                elif st == "RETURNED":
                    plan.notifications.append((f"returned:{aid}:{now[:10]}", "returned", aid,
                                               f"返却: {course.name} {a.title}（再提出要否は Classroom で確認）"))
            if row["missing_since"]:
                changes.append("reappeared")
            elif find_task(task_lines, aid) is None:
                changes.append("task_restore")  # 書込途中で止まった等でタスク行が欠けている

        if changes:
            plan.ops.append({
                "op": "upsert", "assignment_id": aid, "course_id": cid, "changes": changes,
                "note_path": note_path, "assignment": asdict(a),
                "prev_submission": row["submission_state"] if row else None,
            })
        else:
            plan.seen.append(aid)

    if subs_ok:  # 一覧が完全に取れたときだけ「消えた」と判定する
        for row in store.assignments_for_course(cid):
            if not row["source_key"].startswith("classroom:"):
                continue  # メールから登録した課題は Classroom の一覧には出ない
            if row["assignment_id"] not in fetched_ids and not row["missing_since"]:
                plan.missing.append((row["assignment_id"], row["title"]))
                plan.reviews.append((f"missing:{row['assignment_id']}", "一覧から消えた", row["assignment_id"],
                                     f"{course.name} {row['title']} が課題一覧に出なくなった。削除とは断定しない（権限・授業終了・取得条件を確認）。ノートとタスクは残してある"))

    # 資料一覧：資料・お知らせのどちらかが取れなければ、その授業の一覧は更新しない（途中までの一覧で上書きしない）
    op, error = resources.plan(collector, writer, course, works,
                               store.material_copies() if cfg.copy_materials else None)
    plan.sources.append((f"classroom.materials:{cid}", error is None, error or "OK"))
    if op:
        plan.ops.append(op)


def _plan_manual_done_mismatch(store, plan: Plan, task_lines) -> None:
    for line in task_lines:
        m = TASK_RE.match(line)
        if not m or m.group("status") != "x":
            continue
        row = store.assignment(m.group("id"))
        if row and row["submission_state"] not in DONE_STATES:
            plan.reviews.append((f"manual_done:{row['assignment_id']}", "手動完了と学校側が不一致", row["assignment_id"],
                                 f"{row['title']}: タスクは完了だが学校側は「{submission_label(row['submission_state'])}」"))


def _plan_approaching(cfg: Config, store, plan: Plan) -> None:
    if not cfg.approaching_days:
        return
    today = now_jst().date()
    for row in store.all_assignments():
        if not row["due_at"] or row["submission_state"] in DONE_STATES or row["missing_since"]:
            continue
        left = (date.fromisoformat(row["due_at"][:10]) - today).days
        for n in cfg.approaching_days:
            if left == n:
                plan.notifications.append((f"approaching:{row['assignment_id']}:{row['due_at']}:{n}", "approaching", row["assignment_id"],
                                           f"締切まで {n} 日: {row['title']}（{row['due_at']}）"))


# ---------------------------------------------------------------- 適用

def apply_plan(cfg: Config, plan: Plan, store: StateStore, writer: VaultWriter, run_id: str) -> dict:
    now = plan.started_at
    for source, ok, detail in plan.sources:
        store.set_source_status(source, ok, now, detail)
    store.save_ops(run_id, plan.ops)

    results = {"done": 0, "skipped": 0}
    for seq, op in enumerate(plan.ops):
        try:
            if op["op"] == "resources":
                extra_reviews = resources.apply(op, cfg.course_by_id(op["course_id"]), store, writer,
                                                cfg.tasks_note.removesuffix(".md"))
            else:
                extra_reviews = _apply_upsert(cfg, op, store, writer, now)
            for r in extra_reviews:
                plan.reviews.append(r)
            store.mark_op(run_id, seq, "done")
            results["done"] += 1
        except (ConcurrentEditError, GuardError, OSError) as e:
            # 状態は進めない。次回の計画で同じ変更がもう一度出る
            store.mark_op(run_id, seq, "pending", f"{type(e).__name__}: {e}")
            results["skipped"] += 1

    for aid in plan.seen:
        store.touch_assignment(aid, now)
    for aid, _ in plan.missing:
        store.mark_missing(aid, now)
    for rid, kind, aid, detail in plan.reviews:
        store.add_review(rid, kind, aid, detail, now)
    fresh = []
    for eid, kind, aid, msg in plan.notifications:
        if store.add_notification(eid, kind, aid, msg, now):
            fresh.append(msg)
    results["notifications"] = fresh
    return results


def _apply_upsert(cfg: Config, op: dict, store: StateStore, writer: VaultWriter, now: str) -> list:
    reviews = []
    a = assignment_from_dict(op["assignment"])
    aid = a.assignment_id
    course = cfg.course_by_id(op["course_id"])
    row = store.assignment(aid)
    note_rel = op["note_path"]
    stem = Path(note_rel).stem
    block = render_block(a, course, now)
    block_hash = row["block_hash"] if row else None

    # 課題ノート：管理ブロックだけを更新する
    text = writer.read(note_rel)
    if text is None:
        writer.write(note_rel, render_note(a, course, block, cfg.tasks_note.removesuffix(".md")), None)
        block_hash = sha(block)
    elif frontmatter_value(text, "assignment_id") != aid:
        raise GuardError(f"同名の別ノートが既にあるため書かない: {note_rel}")
    else:
        current = extract_block(text)
        if current is None:
            reviews.append((f"conflict_block_missing:{aid}", "管理ブロック消失", aid,
                            f"{note_rel} の管理ブロックが見つからない。学校側の更新を書き込まず保留"))
        elif row is not None and row["block_hash"] and sha(current) != row["block_hash"]:
            reviews.append((f"conflict_block:{aid}:{a.content_hash}", "管理ブロックを本人が編集", aid,
                            f"{note_rel} の自動生成部分が前回出力から変更されている。上書きせず保留"))
        else:
            writer.write(note_rel, replace_block(text, block), text)
            block_hash = sha(block)

    # タスク行：説明・📅・チェック状態だけを更新し、本人の ⏳・優先度は残す
    desc = task_desc(a, course, stem)
    today = now[:10]
    orig_tasks = tasks = writer.read(cfg.tasks_note)
    if tasks is None:
        tasks = "---\ntags: [area, university]\n---\n\n# 🎓 大学\n\n## タスク\n"
    lines = tasks.split("\n")
    idx = find_task(lines, aid)
    if idx is None:
        done = a.submission_state_raw in DONE_STATES and cfg.auto_complete_on_turned_in
        new_tasks = insert_task_line(tasks, render_task_line("x" if done else " ", desc, aid, a.due_date, today if done else None))
        task_desc_written = desc
    else:
        m = TASK_RE.match(lines[idx])
        keep_desc = row is not None and row["task_desc"] is not None and m.group("desc") != row["task_desc"]
        if keep_desc:
            reviews.append((f"conflict_task:{aid}:{a.content_hash}", "タスク行の説明を本人が編集", aid,
                            f"{a.title}: タスク行の説明は本人の編集を残した（期限・状態のみ更新）"))
        status = None
        prev, cur = op["prev_submission"], a.submission_state_raw
        if cur != prev:  # 状態が変わったときだけ動かす。本人のチェック操作を毎回上書きしない
            if cur == "TURNED_IN" and cfg.auto_complete_on_turned_in and m.group("status") == " ":
                status = "x"
            elif cur == "RECLAIMED_BY_STUDENT" and m.group("status") == "x":
                status = " "
        lines[idx] = update_task_line(lines[idx], desc=None if keep_desc else desc, due_date=a.due_date,
                                      status=status, done_date=today,
                                      prev_due=row["due_at"][:10] if row and row["due_at"] else None)
        new_tasks = "\n".join(lines)
        task_desc_written = row["task_desc"] if keep_desc else desc
    writer.write(cfg.tasks_note, new_tasks, orig_tasks)

    store.upsert_assignment({
        "assignment_id": aid, "source_key": a.source_key, "course_id": a.course_id, "title": a.title,
        "content_hash": a.content_hash, "due_at": a.due_at, "due_precision": a.due_precision,
        "submission_state": a.submission_state_raw, "submission_verified_at": a.submission_verified_at,
        "note_path": note_rel, "block_hash": block_hash, "task_desc": task_desc_written,
        "first_seen_at": row["first_seen_at"] if row else now, "last_seen_at": now, "missing_since": None,
        "snapshot": json.dumps(op["assignment"], ensure_ascii=False),
    })
    return reviews


def write_status_notes(cfg: Config, store: StateStore, writer: VaultWriter, mail_enabled: bool = False) -> None:
    from .mailsync import render_candidates_block

    p = paths(cfg)
    pending = len(store.pending_ops())
    reviews = store.open_reviews()
    status_block = render_status_block(store.source_statuses(), store.last_runs(), pending, len(reviews))
    review_block = render_review_block(reviews)
    for rel, title, intro, block, links in [
        (p["status_note"], "大学同期の状況", "university-sync が自動更新する。「取得できず（不明）」は 0 件という意味ではない。",
         status_block, [cfg.tasks_note.removesuffix(".md"), p["review_note"].removesuffix(".md")]),
        (p["review_note"], "大学同期 要確認一覧", "自動登録しなかった候補・競合・一覧から消えた課題。チェックボックスは置かない。",
         review_block, [cfg.tasks_note.removesuffix(".md"), p["status_note"].removesuffix(".md")]),
    ] + ([
        (p["mail_note"], "大学メールの候補", "学校メールから Claude が抽出した課題・連絡の候補。自動では課題に登録しない。"
         "チェックボックスは置かない。", render_candidates_block(store, cfg.school_email),
         [cfg.tasks_note.removesuffix(".md"), p["review_note"].removesuffix(".md")]),
    ] if mail_enabled else []):
        text = writer.read(rel)
        new = wrap_managed(title, intro, block, links) if text is None or extract_block(text) is None else replace_block(text, block)
        if text is None or extract_block(text) is not None:
            writer.write(rel, new, text)


# ---------------------------------------------------------------- 入口

def _run_mail(cfg: Config, store: StateStore, mail_factory, dry_run: bool):
    """メールの段。失敗しても Classroom の同期結果は残す。"""
    from .mailsync import MailReport, sync_mail

    try:
        gmail, llm, drive = mail_factory(store)
    except CollectorError as e:
        return MailReport(ok=False, detail=str(e))
    return sync_mail(cfg, store, gmail, llm, drive, dry_run=dry_run)


def _run_copy(cfg: Config, store: StateStore, collector, copy_factory, dry_run: bool):
    """Classroom の資料を保存先 Drive へ写す段。失敗しても他の同期は続ける。"""
    from .materials import CopyReport, copy_materials

    try:
        reader, drive = copy_factory(store)
    except CollectorError as e:
        return CopyReport(ok=False, detail=str(e))
    return copy_materials(cfg, store, collector, reader, drive, dry_run=dry_run)


def _run_calendar(cfg: Config, store: StateStore, calendar_factory, dry_run: bool):
    """締切をカレンダーへ反映する段。課題の同期を終えた状態ストアから決める。失敗しても他の同期は残す。"""
    from .gcalendar import CalendarReport, sync_calendar

    try:
        cal = calendar_factory(store)
    except CollectorError as e:
        return CalendarReport(ok=False, detail=str(e))
    return sync_calendar(cfg, store, cal, dry_run=dry_run)


def run(cfg: Config, collector: ClassroomCollector, *, dry_run: bool, mail_factory=None,
        copy_factory=None, calendar_factory=None) -> tuple[Plan, dict | None]:
    if cfg.source_mode == "sample" and not dry_run and not cfg.is_test_vault:
        raise SyncBlocked("サンプル取得元で本番 Vault へは書き込まない。テスト用 Vault（目印ファイル付き）を指定するか --dry-run を使う")
    if cfg.source_mode == "google" and not dry_run and not cfg.write_enabled and not cfg.is_test_vault:
        raise SyncBlocked("実データの Vault 書込は Phase 2 で有効にする（sync.write_enabled = true）。今は --dry-run を使う")
    guard = make_guard(cfg)
    if dry_run:
        store = StateStore.open(cfg.state_dir, read_only=True)
        try:
            copy = _run_copy(cfg, store, collector, copy_factory, dry_run=True) if copy_factory else None
            plan = build_plan(cfg, collector, store, VaultWriter(guard, None), "dry-run")
            plan.copy = copy
            if mail_factory:
                plan.mail = _run_mail(cfg, store, mail_factory, dry_run=True)
            if calendar_factory:  # 今回の取得結果を適用する前の状態で数える
                plan.calendar = _run_calendar(cfg, store, calendar_factory, dry_run=True)
            return plan, None
        finally:
            store.close()

    with sync_lock(cfg.state_dir):
        store = StateStore.open(cfg.state_dir)
        try:
            run_id = now_jst().strftime("%Y%m%dT%H%M%S%f")
            superseded = store.begin_run(run_id, iso_now(), "run")
            writer = VaultWriter(guard, cfg.state_dir / "backups" / run_id)
            copy = None
            if copy_factory:  # 先に写しておき、資料一覧に保存コピーのリンクを載せる
                copy = _run_copy(cfg, store, collector, copy_factory, dry_run=False)
                store.set_source_status("drive.materials", copy.ok, iso_now(),
                                        copy.detail or f"新しく写した {copy.copied} 件・コピー不可 {len(copy.restricted)} 件")
            plan = build_plan(cfg, collector, store, writer, "run")
            plan.copy = copy
            if copy and not copy.ok:
                plan.sources.append(("drive.materials", False, copy.detail))
            results = apply_plan(cfg, plan, store, writer, run_id)
            results["superseded_ops"] = superseded
            if mail_factory:
                m = plan.mail = _run_mail(cfg, store, mail_factory, dry_run=False)
                store.set_source_status("gmail", m.ok, iso_now(), m.detail or f"新着 {m.new_ids} 件・対象 {m.relevant} 件・候補 {len(m.candidates)} 件")
                if m.ok:
                    plan.sources.append(("gmail", True, ""))
                else:
                    plan.sources.append(("gmail", False, m.detail))
                for r in m.reviews:
                    store.add_review(*r, iso_now())
                for eid, kind, aid, msg in m.notifications:
                    if store.add_notification(eid, kind, aid, msg, iso_now()):
                        results["notifications"].append(msg)
            if calendar_factory and not plan.fetch_failed:
                c = plan.calendar = _run_calendar(cfg, store, calendar_factory, dry_run=False)
                store.set_source_status("calendar.deadlines", c.ok, iso_now(),
                                        c.detail or f"作成 {c.created} 件・更新 {c.updated} 件・削除 {c.removed} 件")
                if not c.ok:
                    plan.sources.append(("calendar.deadlines", False, c.detail))
                for r in c.reviews:
                    store.add_review(*r, iso_now())
            failed = plan.fetch_failed
            status = "failed" if failed else ("partial" if results["skipped"] or plan.summary()["failed_sources"] else "ok")
            store.finish_run(run_id, iso_now(), status, plan.summary() | {"applied": results["done"], "skipped": results["skipped"]})
            results["status"] = status
            write_status_notes(cfg, store, writer, mail_enabled=mail_factory is not None)
            return plan, results
        finally:
            store.close()


# ---------------------------------------------------------------- メール候補の登録

def accept_candidate(cfg: Config, candidate_id: str, *, due: str | None, course_id: str | None) -> str:
    """要確認のメール候補を本人の判断で課題として登録する。登録先は Classroom の課題と同じ形。"""
    from datetime import datetime as _dt

    from .mail import gmail_link
    from .timeutil import JST

    with sync_lock(cfg.state_dir):
        store = StateStore.open(cfg.state_dir)
        try:
            row = store.candidate(candidate_id)
            if row is None or row["status"] != "open":
                raise SyncBlocked(f"未確認の候補がありません: {candidate_id}")
            c = json.loads(row["data"])
            mail = store.mail(row["message_id"])
            if course_id:
                course = cfg.course_by_id(course_id)
            else:
                matches = [x for x in cfg.courses if x.name in c["course_candidates"]]
                course = matches[0] if len(matches) == 1 else None
            if course is None or not course.vault_dir:
                raise SyncBlocked("授業を一つに決められない。--course <course_id> で指定する")
            if due:
                parsed = _dt.fromisoformat(due)
                due_at = parsed.replace(tzinfo=JST).isoformat(timespec="minutes") if "T" in due else due
                precision = "datetime" if "T" in due else "date"
            elif len(c["deadline_candidates"]) == 1:
                d = c["deadline_candidates"][0]
                due_at = f"{d['date']}T{d['time']}+09:00" if d["time"] else d["date"]
                precision = d["precision"]
            elif not c["deadline_candidates"]:
                due_at, precision = None, "unknown"
            else:
                raise SyncBlocked("期限の候補が複数ある。--due YYYY-MM-DD または YYYY-MM-DDTHH:MM で指定する")
            account = store.kv_get("gmail:account_id") or "unknown"
            link = gmail_link(cfg.school_email, row["message_id"])
            atts = [{"kind": "drive", "title": a["filename"], "url": a["link"]}
                    for a in store.attachments_for(row["message_id"]) if a["link"]]
            now = iso_now()
            a = Assignment(
                source_key=f"gmail:{account}:{row['message_id']}:{row['idx']}", course_id=course.course_id,
                coursework_id="", title=c["title"], description=c.get("evidence_text", ""), url=link, work_type="MAIL",
                due_at=due_at, due_precision=precision, due_raw={"mail": c["deadline_candidates"], "override": due},
                source_updated_at=mail["received_at"] if mail else None, materials=atts,
                content_hash=sha(json.dumps(c, ensure_ascii=False, sort_keys=True)),
                source_refs=[SourceRef("gmail", row["message_id"], link, now, c.get("evidence_text", ""))],
            )
            aid = a.assignment_id
            note = f"{course.vault_dir}/課題/{safe_filename(a.title)}_{aid[3:9]}.md"
            op = {"op": "upsert", "assignment_id": aid, "course_id": course.course_id, "changes": ["new"],
                  "note_path": note, "assignment": asdict(a), "prev_submission": None}
            run_id = now_jst().strftime("%Y%m%dT%H%M%S%f")
            writer = VaultWriter(make_guard(cfg), cfg.state_dir / "backups" / run_id)
            _apply_upsert(cfg, op, store, writer, now)
            store.set_candidate_status(candidate_id, "accepted", aid)
            write_status_notes(cfg, store, writer, mail_enabled=True)
            return note
        finally:
            store.close()


def dismiss_candidate(cfg: Config, candidate_id: str) -> bool:
    with sync_lock(cfg.state_dir):
        store = StateStore.open(cfg.state_dir)
        try:
            row = store.candidate(candidate_id)
            if row is None or row["status"] != "open":
                return False
            store.set_candidate_status(candidate_id, "dismissed")
            write_status_notes(cfg, store, VaultWriter(make_guard(cfg), None), mail_enabled=True)
            return True
        finally:
            store.close()


def record_blocked(cfg: Config, reason: str) -> None:
    """認可切れなどで同期を始められなかったことを、同期状況のノートに残す（無人実行で気づけるように）。"""
    if not cfg.state_dir.exists():
        return
    with sync_lock(cfg.state_dir):
        store = StateStore.open(cfg.state_dir)
        try:
            now = iso_now()
            store.set_source_status("classroom.courses", False, now, reason)
            store.add_notification(f"blocked:{now[:10]}:{sha(reason)}", "fetch_failed", None, f"同期できず: {reason}", now)
            run_id = now_jst().strftime("%Y%m%dT%H%M%S%f")
            store.begin_run(run_id, now, "run")
            store.finish_run(run_id, iso_now(), "failed", {"blocked": reason})
            if cfg.write_enabled or cfg.is_test_vault:
                write_status_notes(cfg, store, VaultWriter(make_guard(cfg), None), mail_enabled=bool(cfg.sender_domains))
        finally:
            store.close()


def last_success_age_hours(cfg: Config) -> float | None:
    if not (cfg.state_dir / "state.sqlite3").exists():
        return None
    store = StateStore.open(cfg.state_dir, read_only=True)
    try:
        row = store.conn.execute("SELECT MAX(finished_at) AS t FROM runs WHERE mode='run' AND status IN ('ok','partial')").fetchone()
    finally:
        store.close()
    if not row or not row["t"]:
        return None
    from datetime import datetime
    return (now_jst() - datetime.fromisoformat(row["t"])).total_seconds() / 3600
