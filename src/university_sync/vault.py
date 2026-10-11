"""Vault への安全な書き込み。

- 書込先は許可リストの範囲内だけ。`Note/` と `タスク置き場.md` はプログラム側でも拒否する。
- 単一ファイルは一時ファイル＋置換で書き、途中まで書かれた状態を残さない。
- 書込直前に内容を読み直し、計画時から他の編集が入っていれば中止する。
- 管理ブロック（university-sync:start〜end）とタスク行の管理部分だけを更新する。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

BLOCK_START = "<!-- university-sync:start -->"
BLOCK_END = "<!-- university-sync:end -->"
BLOCK_RE = re.compile(re.escape(BLOCK_START) + r"\n(.*?)" + re.escape(BLOCK_END), re.S)
TASK_RE = re.compile(
    r"^(?P<indent>\s*)- \[(?P<status>.)\] (?P<desc>.*?)\s*"
    r"<!-- university-sync:id=(?P<id>ua-[0-9a-f]+) -->(?P<rest>.*)$"
)
DUE_RE = re.compile(r"\s*📅 \d{4}-\d{2}-\d{2}")
DONE_RE = re.compile(r"\s*✅ \d{4}-\d{2}-\d{2}")


class GuardError(Exception):
    pass


class ConcurrentEditError(Exception):
    pass


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def task_marker(assignment_id: str) -> str:
    return f"<!-- university-sync:id={assignment_id} -->"


def sanitize_external(text: str) -> str:
    """学校側の本文をデータとして埋め込む。管理マーカーや見出しの偽装を無効化する。"""
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return "\n".join("> " + line if line else ">" for line in text.splitlines()) or "> （本文なし）"


def safe_filename(title: str, limit: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|#^\[\]\n\r\t]', "_", nfc(title)).strip(" .")
    return (s[:limit] or "無題").rstrip(" .")


class VaultGuard:
    def __init__(self, root: Path, allowed: list[str], protected: list[str]):
        self.root = root.resolve()
        self.allowed = [nfc(a) for a in allowed]
        self.protected = [nfc(p) for p in protected]

    def rel(self, path: Path) -> str:
        return nfc(path.resolve().relative_to(self.root).as_posix())

    def check(self, rel: str) -> Path:
        target = (self.root / rel).resolve()
        try:
            r = nfc(target.relative_to(self.root).as_posix())
        except ValueError:
            raise GuardError(f"Vault 外への書き込みを拒否: {rel}")
        for p in self.protected:
            if r == p.rstrip("/") or (p.endswith("/") and r.startswith(p)):
                raise GuardError(f"保護領域への書き込みを拒否: {r}")
        for a in self.allowed:
            if r == a or (a.endswith("/") and r.startswith(a)):
                return target
        raise GuardError(f"許可範囲外への書き込みを拒否: {r}")


class VaultWriter:
    def __init__(self, guard: VaultGuard, backup_dir: Path | None):
        self.guard = guard
        self.backup_dir = backup_dir

    def read(self, rel: str) -> str | None:
        p = self.guard.root / rel
        return p.read_text(encoding="utf-8") if p.exists() else None

    def write(self, rel: str, content: str, expected_before: str | None) -> None:
        """expected_before は計画時に読んだ内容（新規なら None）。食い違えば書かない。"""
        target = self.guard.check(rel)
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current != expected_before:
            raise ConcurrentEditError(f"計画後に編集されています: {rel}")
        if current == content:
            return
        if current is not None and self.backup_dir is not None:
            b = self.backup_dir / rel
            if not b.exists():  # 同じ実行で何度書いても、最初（実行前）の内容だけを残す
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, b)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".usync-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


# --- 管理ブロック ---

def extract_block(text: str) -> str | None:
    m = BLOCK_RE.search(text)
    return m.group(1) if m else None


def replace_block(text: str, new_inner: str) -> str:
    return BLOCK_RE.sub(lambda _: f"{BLOCK_START}\n{new_inner}{BLOCK_END}", text, count=1)


def frontmatter_value(text: str, key: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    for line in text[4:end].splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip('"')
    return None


# --- タスク行 ---

def find_task(lines: list[str], assignment_id: str) -> int | None:
    for i, line in enumerate(lines):
        m = TASK_RE.match(line)
        if m and m.group("id") == assignment_id:
            return i
    return None


def render_task_line(status: str, desc: str, assignment_id: str, due_date: str | None, done_date: str | None = None) -> str:
    """Tasks プラグインは行末からフィールドを読むため、管理マーカーは説明側に置き、
    タグと日付を最後に並べる。"""
    line = f"- [{status}] {desc} {task_marker(assignment_id)} #university"
    if due_date:
        line += f" 📅 {due_date}"
    if done_date:
        line += f" ✅ {done_date}"
    return line


def update_task_line(line: str, *, desc: str | None, due_date: str | None, status: str | None, done_date: str | None) -> str:
    """既存行のうち、管理部分（説明・📅・チェック状態）だけを差し替える。
    本人が足した ⏳ や優先度、タグはそのまま残す。desc=None なら説明を変えない。"""
    m = TASK_RE.match(line)
    assert m
    rest = DUE_RE.sub("", m.group("rest"))
    if due_date:
        if "#university" in rest:
            rest = rest.replace("#university", f"#university 📅 {due_date}", 1)
        else:
            rest += f" 📅 {due_date}"
    new_status = m.group("status") if status is None else status
    if status is not None and status != m.group("status"):
        rest = DONE_RE.sub("", rest)
        if status == "x" and done_date:
            rest += f" ✅ {done_date}"
    d = m.group("desc") if desc is None else desc
    return f"{m.group('indent')}- [{new_status}] {d} {task_marker(m.group('id'))}{rest}"


def insert_task_line(text: str, new_line: str, section: str = "## タスク") -> str:
    """`## タスク` 節の最後のチェックボックス行の直後に追加する。節が無ければ末尾に作る。"""
    lines = text.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == section)
    except StopIteration:
        sep = "" if text.endswith("\n") else "\n"
        return f"{text}{sep}\n{section}\n{new_line}\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("#"):
            end = j
            break
    insert_at = start + 1
    for j in range(start + 1, end):
        if re.match(r"^\s*- \[.\] ", lines[j]):
            insert_at = j + 1
    lines.insert(insert_at, new_line)
    return "\n".join(lines)
