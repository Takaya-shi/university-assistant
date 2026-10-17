"""Vault 外の状態ストア（SQLite）。課題ID・変更指紋・同期状況・書込待ち・通知履歴を持つ。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
  assignment_id TEXT PRIMARY KEY,
  source_key TEXT UNIQUE NOT NULL,
  course_id TEXT NOT NULL,
  title TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  due_at TEXT,
  due_precision TEXT NOT NULL,
  submission_state TEXT,
  submission_verified_at TEXT,
  note_path TEXT NOT NULL,
  block_hash TEXT,          -- 前回書いた管理ブロックの指紋（本人編集の検知用）
  task_desc TEXT,           -- 前回書いたタスク行の説明部分
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  missing_since TEXT,
  snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL        -- Gmail の history ID、アプリが作った Drive フォルダ ID など
);
CREATE TABLE IF NOT EXISTS mail_messages (
  message_id TEXT PRIMARY KEY,
  thread_id TEXT,
  received_at TEXT,
  sender TEXT,
  subject TEXT,
  kind TEXT,                 -- classroom / university / ignored
  status TEXT NOT NULL,      -- new / pending / done / ignored / failed
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  first_seen_at TEXT NOT NULL,
  processed_at TEXT
);
CREATE TABLE IF NOT EXISTS mail_candidates (
  candidate_id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  data TEXT NOT NULL,
  status TEXT NOT NULL,      -- open / accepted / dismissed
  assignment_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_attachments (
  message_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  drive_file_id TEXT,
  link TEXT,
  status TEXT NOT NULL,      -- saved / failed / skipped
  detail TEXT,
  PRIMARY KEY (message_id, filename)
);
CREATE TABLE IF NOT EXISTS mail_links (
  message_id TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  PRIMARY KEY (message_id, assignment_id)
);
CREATE TABLE IF NOT EXISTS material_copies (
  source_id TEXT PRIMARY KEY,  -- 学校 Drive の元ファイル ID
  course_id TEXT NOT NULL,
  name TEXT,
  source_mime TEXT,
  source_modified TEXT,        -- 元の更新日時。変わったら新しいコピーを作る
  copy_id TEXT,
  link TEXT,
  status TEXT NOT NULL,        -- saved / restricted / failed
  detail TEXT,
  copied_at TEXT
);
CREATE TABLE IF NOT EXISTS calendar_events (
  assignment_id TEXT PRIMARY KEY,
  calendar_id TEXT NOT NULL,
  event_id TEXT,
  fingerprint TEXT,            -- 前回書いた件名・開始・終了（本人編集の検知用）
  status TEXT NOT NULL,        -- active / done / removed / user_deleted / user_edited
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS calendar_tasks (
  task_key TEXT PRIMARY KEY,   -- 手書きのタスク行の指紋（txt:…）か、締切の無い管理課題（aid:…）
  calendar_id TEXT NOT NULL,
  event_id TEXT,
  fingerprint TEXT,
  status TEXT NOT NULL,        -- active / done / removed / user_deleted / user_edited / external
  title TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS exams (
  exam_key TEXT PRIMARY KEY,   -- exam:<course_id>:<YYYY-MM-DD>:<呼び名>
  course_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  exam_date TEXT NOT NULL,
  start_time TEXT,             -- HH:MM（本文に無ければ NULL）
  end_time TEXT,
  in_class INTEGER NOT NULL,   -- 授業時間に行うと本文に書かれている
  source TEXT NOT NULL,        -- classroom-announcement:<id> / mail:<candidate_id>
  evidence TEXT,
  first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS class_removals (
  event_id TEXT PRIMARY KEY,   -- 授業カレンダーから削除した回の予定 ID
  exam_key TEXT NOT NULL,
  summary TEXT,
  start TEXT,
  end TEXT,
  removed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_lists (
  course_id TEXT PRIMARY KEY,
  note_path TEXT NOT NULL,
  block_hash TEXT NOT NULL   -- 前回書いた資料一覧の管理ブロックの指紋
);
CREATE TABLE IF NOT EXISTS source_status (
  source TEXT PRIMARY KEY,
  status TEXT NOT NULL,      -- ok / failed / not_configured
  last_attempt_at TEXT,
  last_success_at TEXT,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,      -- running / ok / partial / failed
  summary TEXT
);
CREATE TABLE IF NOT EXISTS pending_ops (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  op TEXT NOT NULL,
  status TEXT NOT NULL,      -- pending / done / skipped / superseded
  detail TEXT,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS notifications (
  event_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  assignment_id TEXT,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  assignment_id TEXT,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
"""


class StateStore:
    def __init__(self, conn: sqlite3.Connection, read_only: bool):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.read_only = read_only

    @classmethod
    def open(cls, state_dir: Path, read_only: bool = False) -> "StateStore":
        db = state_dir / "state.sqlite3"
        if read_only:
            # dry-run は本番状態を一切変更しない。DB が無ければ空のメモリ DB で代用する
            # 本番の DB は読み取り専用で開いてメモリへ写す。古い版の DB でも新しいテーブルを足して読めるようにする
            conn = sqlite3.connect(":memory:")
            if db.exists():
                src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                src.backup(conn)
                src.close()
            conn.executescript(SCHEMA)
            return cls(conn, True)
        state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        return cls(conn, False)

    def close(self) -> None:
        self.conn.close()

    # --- 読み取り ---
    def assignment(self, assignment_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM assignments WHERE assignment_id=?", (assignment_id,)).fetchone()

    def assignments_for_course(self, course_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM assignments WHERE course_id=?", (course_id,)).fetchall()

    def all_assignments(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM assignments ORDER BY due_at IS NULL, due_at").fetchall()

    def resource_list(self, course_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM resource_lists WHERE course_id=?", (course_id,)).fetchone()

    def has_successful_run(self) -> bool:
        return self.conn.execute("SELECT 1 FROM runs WHERE status IN ('ok','partial') AND mode='run' LIMIT 1").fetchone() is not None

    def source_statuses(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM source_status ORDER BY source").fetchall()

    def last_runs(self, n: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (n,)).fetchall()

    def open_reviews(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reviews WHERE resolved_at IS NULL ORDER BY created_at").fetchall()

    def pending_ops(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM pending_ops WHERE status='pending' ORDER BY run_id, seq").fetchall()

    def notification_exists(self, event_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM notifications WHERE event_id=?", (event_id,)).fetchone() is not None

    # --- 書き込み ---
    def _w(self) -> None:
        if self.read_only:
            raise RuntimeError("read-only の状態ストアに書き込もうとしました（dry-run の不具合）")

    def begin_run(self, run_id: str, started_at: str, mode: str) -> int:
        """新しい実行を記録し、前回までの未完了書込を superseded にした件数を返す。"""
        self._w()
        cur = self.conn.execute("UPDATE pending_ops SET status='superseded' WHERE status='pending'")
        self.conn.execute("UPDATE runs SET status='failed', summary=COALESCE(summary,'中断') WHERE status='running'")
        self.conn.execute("INSERT INTO runs(run_id, started_at, mode, status) VALUES (?,?,?, 'running')", (run_id, started_at, mode))
        self.conn.commit()
        return cur.rowcount

    def finish_run(self, run_id: str, finished_at: str, status: str, summary: dict) -> None:
        self._w()
        self.conn.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE run_id=?",
                          (finished_at, status, json.dumps(summary, ensure_ascii=False), run_id))
        self.conn.commit()

    def save_ops(self, run_id: str, ops: list[dict]) -> None:
        self._w()
        self.conn.executemany("INSERT INTO pending_ops(run_id, seq, op, status) VALUES (?,?,?, 'pending')",
                              [(run_id, i, json.dumps(op, ensure_ascii=False)) for i, op in enumerate(ops)])
        self.conn.commit()

    def mark_op(self, run_id: str, seq: int, status: str, detail: str = "") -> None:
        self._w()
        self.conn.execute("UPDATE pending_ops SET status=?, detail=? WHERE run_id=? AND seq=?", (status, detail, run_id, seq))
        self.conn.commit()

    def upsert_assignment(self, row: dict) -> None:
        self._w()
        cols = list(row)
        sets = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("assignment_id", "first_seen_at"))
        self.conn.execute(
            f"INSERT INTO assignments({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(assignment_id) DO UPDATE SET {sets}",
            [row[c] for c in cols],
        )
        self.conn.commit()

    def set_resource_list(self, course_id: str, note_path: str, block_hash: str) -> None:
        self._w()
        self.conn.execute(
            "INSERT INTO resource_lists(course_id,note_path,block_hash) VALUES (?,?,?) "
            "ON CONFLICT(course_id) DO UPDATE SET note_path=excluded.note_path, block_hash=excluded.block_hash",
            (course_id, note_path, block_hash))
        self.conn.commit()

    def touch_assignment(self, assignment_id: str, seen_at: str) -> None:
        self._w()
        self.conn.execute("UPDATE assignments SET last_seen_at=?, missing_since=NULL WHERE assignment_id=?", (seen_at, assignment_id))
        self.conn.commit()

    def mark_missing(self, assignment_id: str, since: str) -> None:
        self._w()
        self.conn.execute("UPDATE assignments SET missing_since=COALESCE(missing_since, ?) WHERE assignment_id=?", (since, assignment_id))
        self.conn.commit()

    def set_source_status(self, source: str, ok: bool, at: str, detail: str = "") -> None:
        self._w()
        if ok:
            self.conn.execute(
                "INSERT INTO source_status(source,status,last_attempt_at,last_success_at,detail) VALUES (?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET status=excluded.status, last_attempt_at=excluded.last_attempt_at, "
                "last_success_at=excluded.last_success_at, detail=excluded.detail",
                (source, "ok", at, at, detail))
        else:
            self.conn.execute(
                "INSERT INTO source_status(source,status,last_attempt_at,detail) VALUES (?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET status=excluded.status, last_attempt_at=excluded.last_attempt_at, "
                "detail=excluded.detail",
                (source, "failed", at, detail))
        self.conn.commit()

    def add_review(self, review_id: str, kind: str, assignment_id: str | None, detail: str, at: str) -> None:
        self._w()
        self.conn.execute(
            "INSERT INTO reviews(review_id,kind,assignment_id,detail,created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(review_id) DO UPDATE SET detail=excluded.detail",
            (review_id, kind, assignment_id, detail, at))
        self.conn.commit()

    def resolve_review(self, review_id: str, at: str) -> bool:
        self._w()
        cur = self.conn.execute("UPDATE reviews SET resolved_at=? WHERE review_id=? AND resolved_at IS NULL", (at, review_id))
        self.conn.commit()
        return cur.rowcount > 0

    def add_notification(self, event_id: str, kind: str, assignment_id: str | None, message: str, at: str) -> bool:
        self._w()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO notifications(event_id,kind,assignment_id,message,created_at) VALUES (?,?,?,?,?)",
            (event_id, kind, assignment_id, message, at))
        self.conn.commit()
        return cur.rowcount > 0

    # --- メール（Phase 3） ---
    def kv_get(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def kv_set(self, key: str, value: str) -> None:
        self._w()
        self.conn.execute("INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def mail(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM mail_messages WHERE message_id=?", (message_id,)).fetchone()

    def add_mail_ids(self, ids: list[str], at: str) -> int:
        self._w()
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO mail_messages(message_id,status,first_seen_at) VALUES (?, 'new', ?)", [(i, at) for i in ids])
        self.conn.commit()
        return cur.rowcount

    def mails_to_process(self, max_attempts: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM mail_messages WHERE status IN ('new','pending') AND attempts < ? "
                                 "ORDER BY first_seen_at", (max_attempts,)).fetchall()

    def update_mail(self, message_id: str, **fields) -> None:
        self._w()
        cols = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE mail_messages SET {cols} WHERE message_id=?", [*fields.values(), message_id])
        self.conn.commit()

    def add_candidate(self, candidate_id: str, message_id: str, idx: int, data: dict, at: str) -> bool:
        self._w()
        cur = self.conn.execute("INSERT OR IGNORE INTO mail_candidates(candidate_id,message_id,idx,data,status,created_at) "
                                "VALUES (?,?,?,?, 'open', ?)", (candidate_id, message_id, idx, json.dumps(data, ensure_ascii=False), at))
        self.conn.commit()
        return cur.rowcount > 0

    def candidate(self, candidate_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM mail_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()

    def open_candidates(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, m.subject, m.sender, m.received_at FROM mail_candidates c JOIN mail_messages m USING(message_id) "
            "WHERE c.status='open' ORDER BY m.received_at DESC, c.idx").fetchall()

    def set_candidate_status(self, candidate_id: str, status: str, assignment_id: str | None = None) -> None:
        self._w()
        self.conn.execute("UPDATE mail_candidates SET status=?, assignment_id=? WHERE candidate_id=?", (status, assignment_id, candidate_id))
        self.conn.commit()

    def attachments_for(self, message_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM mail_attachments WHERE message_id=?", (message_id,)).fetchall()

    def set_attachment(self, message_id: str, filename: str, status: str, drive_file_id=None, link=None, detail="") -> None:
        self._w()
        self.conn.execute(
            "INSERT INTO mail_attachments(message_id,filename,drive_file_id,link,status,detail) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(message_id,filename) DO UPDATE SET drive_file_id=excluded.drive_file_id, link=excluded.link, "
            "status=excluded.status, detail=excluded.detail", (message_id, filename, drive_file_id, link, status, detail))
        self.conn.commit()

    def link_mail(self, message_id: str, assignment_id: str) -> None:
        self._w()
        self.conn.execute("INSERT OR IGNORE INTO mail_links(message_id,assignment_id) VALUES (?,?)", (message_id, assignment_id))
        self.conn.commit()

    def assignment_by_source(self, source_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM assignments WHERE source_key=?", (source_key,)).fetchone()

    # --- 締切カレンダー（Phase 6） ---
    def calendar_event(self, assignment_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM calendar_events WHERE assignment_id=?", (assignment_id,)).fetchone()

    def set_calendar_event(self, assignment_id: str, calendar_id: str, event_id: str | None, fingerprint: str | None,
                           status: str, at: str) -> None:
        self._w()
        self.conn.execute(
            "INSERT INTO calendar_events(assignment_id,calendar_id,event_id,fingerprint,status,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(assignment_id) DO UPDATE SET calendar_id=excluded.calendar_id, event_id=excluded.event_id, "
            "fingerprint=excluded.fingerprint, status=excluded.status, updated_at=excluded.updated_at",
            (assignment_id, calendar_id, event_id, fingerprint, status, at))
        self.conn.commit()

    def calendar_task(self, task_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM calendar_tasks WHERE task_key=?", (task_key,)).fetchone()

    def calendar_tasks(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM calendar_tasks").fetchall()

    def set_calendar_task(self, task_key: str, calendar_id: str, event_id: str | None, fingerprint: str | None,
                          status: str, title: str, at: str) -> None:
        self._w()
        self.conn.execute(
            "INSERT INTO calendar_tasks(task_key,calendar_id,event_id,fingerprint,status,title,updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(task_key) DO UPDATE SET calendar_id=excluded.calendar_id, event_id=excluded.event_id, "
            "fingerprint=excluded.fingerprint, status=excluded.status, title=excluded.title, updated_at=excluded.updated_at",
            (task_key, calendar_id, event_id, fingerprint, status, title, at))
        self.conn.commit()

    # --- 試験（Phase 6） ---
    def upsert_exam(self, row: dict) -> None:
        self._w()
        cols = list(row)
        sets = ",".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in cols if c not in ("exam_key", "first_seen_at"))
        self.conn.execute(f"INSERT INTO exams({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                          f"ON CONFLICT(exam_key) DO UPDATE SET {sets}", [row[c] for c in cols])
        self.conn.commit()

    def all_exams(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM exams ORDER BY exam_date").fetchall()

    def set_exam_time(self, exam_key: str, start: str, end: str) -> None:
        """授業の時間・本人の予定から決めた時刻を残す（授業の回を消した後も同じ時刻で扱う）。"""
        self._w()
        self.conn.execute("UPDATE exams SET start_time=?, end_time=? WHERE exam_key=? AND start_time IS NULL",
                          (start, end, exam_key))
        self.conn.commit()

    def class_removal(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM class_removals WHERE event_id=?", (event_id,)).fetchone()

    def add_class_removal(self, event_id: str, exam_key: str, summary: str, start: str, end: str, at: str) -> None:
        self._w()
        self.conn.execute("INSERT OR IGNORE INTO class_removals(event_id,exam_key,summary,start,end,removed_at) VALUES (?,?,?,?,?,?)",
                          (event_id, exam_key, summary, start, end, at))
        self.conn.commit()

    # --- Classroom 資料のコピー（Phase 5） ---
    def material_copy(self, source_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM material_copies WHERE source_id=?", (source_id,)).fetchone()

    def material_copies(self) -> dict[str, sqlite3.Row]:
        return {r["source_id"]: r for r in self.conn.execute("SELECT * FROM material_copies").fetchall()}

    def set_material_copy(self, source_id: str, **f) -> None:
        self._w()
        cols = ["source_id", *f]
        self.conn.execute(
            f"INSERT INTO material_copies({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(source_id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in f)}",
            [source_id, *f.values()])
        self.conn.commit()
