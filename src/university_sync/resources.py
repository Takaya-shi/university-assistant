"""授業ごとの資料一覧（設計書 4.2 の `資料一覧.md`）。

資料の投稿・お知らせの添付・課題の添付をまとめ、Drive 等の元リンクを並べる。
ファイル本体はコピーしない。タスクにもしない。
管理ブロックだけを更新し、本人がブロックを編集していたら上書きせず要確認へ回す。
"""

from __future__ import annotations

from datetime import datetime

from .collectors import CollectorError, paginate
from .config import CourseMapping
from .normalize import _material_summary
from .timeutil import JST
from .vault import (BLOCK_END, BLOCK_START, GuardError, VaultWriter, extract_block, frontmatter_value,
                    replace_block, sanitize_external, sha)

ANNOUNCEMENT_CHARS = 300


def note_path(course: CourseMapping) -> str:
    return f"{course.vault_dir}/資料一覧.md"


def _date(ts: str | None) -> str:
    if not ts:
        return "—"
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST).strftime("%Y-%m-%d")


def _cell(s: str) -> str:
    return " ".join(s.split()).replace("|", "｜")


def _link(m: dict) -> str:
    t = _cell(m.get("title") or m.get("url") or "資料").replace("[", "［").replace("]", "］")
    return f"[{t}]({m['url']})" if m.get("url") else t


def collect(collector, course_id: str, works: list[dict]) -> tuple[list[dict], list[dict]]:
    """(資料の行, お知らせ) を返す。取得に失敗したら CollectorError。"""
    posts = list(paginate(lambda t: collector.materials_page(course_id, t)))
    anns = list(paginate(lambda t: collector.announcements_page(course_id, t)))
    rows = []
    for p in posts:
        for m in p.get("materials", []):
            rows.append({"date": _date(p.get("creationTime")), "material": _material_summary(m),
                         "source": f"資料：{_cell(p.get('title', ''))}"})
    for a in anns:
        for m in a.get("materials", []):
            rows.append({"date": _date(a.get("creationTime")), "material": _material_summary(m), "source": "お知らせ"})
    for w in works:
        for m in w.get("materials", []):
            rows.append({"date": _date(w.get("creationTime")), "material": _material_summary(m),
                         "source": f"課題：{_cell(w.get('title', ''))}"})
    rows.sort(key=lambda r: (r["date"], r["source"], r["material"].get("title", "")), reverse=True)
    anns.sort(key=lambda a: a.get("creationTime", ""), reverse=True)
    return rows, anns


def _copy_cell(m: dict, copies: dict | None) -> str:
    if copies is None:
        return ""
    c = copies.get(m.get("id")) if m.get("kind") == "drive" else None
    if c is None:
        return " — |"
    if c["status"] == "saved" and c["link"]:
        return f" [コピー]({c['link']}) |"
    if c["status"] == "restricted":
        return " コピー不可（制限） |" if not c["link"] else f" [旧コピー]({c['link']})・最新はコピー不可 |"
    return " 未保存（再試行待ち） |"


def render_block(rows: list[dict], anns: list[dict], copies: dict | None = None) -> str:
    """取得日時を入れない。中身が変わらなければ毎日の同期で書き換えない。
    copies を渡すと保存先 Drive に写した保存コピーの列を足す。"""
    lines = ["## 資料（新しい順）", ""]
    if rows:
        if copies is None:
            lines += ["| 投稿日 | 資料 | 掲載元 |", "| --- | --- | --- |"]
        else:
            lines += ["| 投稿日 | 資料 | 掲載元 | 保存コピー |", "| --- | --- | --- | --- |"]
        lines += [f"| {r['date']} | {_link(r['material'])} | {r['source']} |{_copy_cell(r['material'], copies)}" for r in rows]
    else:
        lines.append("資料はまだありません。")
    lines += ["", "## お知らせ（新しい順）", ""]
    if not anns:
        lines.append("お知らせはまだありません。")
    for a in anns:
        body = (a.get("text") or "").strip()
        if len(body) > ANNOUNCEMENT_CHARS:
            body = body[:ANNOUNCEMENT_CHARS] + "…（続きは Classroom）"
        lines += [f"### {_date(a.get('creationTime'))}", "", sanitize_external(body), ""]
        if a.get("materials"):
            lines.append("添付: " + "、".join(_link(_material_summary(m)) for m in a["materials"]))
            lines.append("")
        if a.get("alternateLink"):
            lines += [f"[Classroom で開く]({a['alternateLink']})", ""]
    return "\n".join(lines) + "\n"


def render_note(course: CourseMapping, block: str, tasks_link: str) -> str:
    return f"""---
type: course-materials
course_id: "{course.course_id}"
course: "{course.name}"
tags: [university]
---

# {course.name} 資料一覧

Classroom の資料・お知らせの添付・課題の添付へのリンク。ファイル本体は元の Drive にある。下の自動生成部分は university-sync が更新する。

{BLOCK_START}
{block}{BLOCK_END}

## 自分のメモ

## 関連ノート

- [[{tasks_link}]]
- [[{course.vault_dir}/00_科目ホーム|{course.name}]]
"""


def plan(collector, writer: VaultWriter, course: CourseMapping, works: list[dict],
         copies: dict | None = None) -> tuple[dict | None, str | None]:
    """(op, 失敗理由)。変更がなければ op は None。"""
    try:
        rows, anns = collect(collector, course.course_id, works)
    except CollectorError as e:
        return None, str(e)
    block = render_block(rows, anns, copies)
    rel = note_path(course)
    text = writer.read(rel)
    if text is not None and extract_block(text) == block:
        return None, None
    if not rows and not anns and text is None:
        return None, None  # 何も無い授業には空の一覧を作らない
    return {"op": "resources", "course_id": course.course_id, "note_path": rel, "block": block,
            "changes": ["resources"], "count": len(rows), "announcements": len(anns)}, None


def apply(op: dict, course: CourseMapping, store, writer: VaultWriter, tasks_link: str) -> list:
    rel, block = op["note_path"], op["block"]
    row = store.resource_list(course.course_id)
    text = writer.read(rel)
    if text is None:
        writer.write(rel, render_note(course, block, tasks_link), None)
    elif frontmatter_value(text, "course_id") != course.course_id:
        raise GuardError(f"同名の別ノートが既にあるため書かない: {rel}")
    else:
        current = extract_block(text)
        if current is None or (row is not None and sha(current) != row["block_hash"]):
            return [(f"conflict_resources:{course.course_id}:{sha(block)}", "資料一覧の自動生成部分を本人が編集", None,
                     f"{rel} の自動生成部分が前回出力と違う。上書きせず保留")]
        writer.write(rel, replace_block(text, block), text)
    store.set_resource_list(course.course_id, rel, sha(block))
    return []
