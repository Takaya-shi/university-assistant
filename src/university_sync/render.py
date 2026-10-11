"""Markdown 生成。課題ノート・タスク行の説明・同期状況・要確認一覧。"""

from __future__ import annotations

from .config import CourseMapping
from .models import Assignment
from .vault import BLOCK_END, BLOCK_START, sanitize_external

SUBMISSION_LABELS = {
    None: "未取得",
    "SUBMISSION_STATE_UNSPECIFIED": "不明",
    "NEW": "未提出",
    "CREATED": "未提出",
    "TURNED_IN": "提出済み",
    "RETURNED": "返却済み",
    "RECLAIMED_BY_STUDENT": "提出取り消し",
}


def submission_label(state: str | None) -> str:
    return SUBMISSION_LABELS.get(state, f"不明（{state}）")


def due_label(a: Assignment) -> str:
    if a.due_precision == "datetime":
        return f"{a.due_date} {a.due_time}（JST）"
    if a.due_precision == "date":
        return f"{a.due_date}（時刻の記載なし）"
    return "締切未設定"


def task_desc(a: Assignment, course: CourseMapping, note_stem: str) -> str:
    if a.due_precision == "datetime":
        when = f"（締切{a.due_time}）"
    elif a.due_precision == "date":
        when = ""
    else:
        when = "（締切未設定）"
    title = a.title.replace("[[", "［［").replace("]]", "］］")
    return f"{course.name} {title}提出{when} [[{note_stem}]]"


def render_block(a: Assignment, course: CourseMapping, written_at: str) -> str:
    lines = [
        "## 学校からの課題情報",
        "",
        f"- 科目: {course.name}",
        f"- 締切: {due_label(a)}",
        f"- 提出状態（学校側）: {submission_label(a.submission_state_raw)}"
        + (f"・遅延" if a.submission_late else "")
        + (f"（確認 {a.submission_verified_at}）" if a.submission_verified_at else ""),
        f"- Classroom: {a.url or '（リンクなし）'}",
        f"- 情報の更新: {written_at}",
        "",
        "### 課題内容（学校の本文・引用）",
        "",
        sanitize_external(a.description),
        "",
    ]
    if a.materials:
        lines += ["### 参照資料", ""]
        for m in a.materials:
            t = (m.get("title") or m.get("url") or "資料").replace("]", "］")
            lines.append(f"- [{t}]({m['url']})" if m.get("url") else f"- {t}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_note(a: Assignment, course: CourseMapping, block: str, tasks_note_link: str) -> str:
    title = a.title.replace("\n", " ")
    return f"""---
type: assignment
assignment_id: {a.assignment_id}
course: "{course.name}"
local_work_status: 未着手
---

# {title}

{BLOCK_START}
{block}{BLOCK_END}

## 自分の回答

## 考え方・途中計算

## AIの解説・助言

## 見直し・復習ポイント

## 提出後のフィードバック

## 関連ノート

- [[{tasks_note_link}]]
- [[{course.vault_dir}/00_科目ホーム|{course.name}]]
"""


def wrap_managed(title: str, intro: str, block: str, links: list[str]) -> str:
    rel = "\n".join(f"- [[{l}]]" for l in links)
    return f"""---
tags: [university, sync]
---

# {title}

{intro}

{BLOCK_START}
{block}{BLOCK_END}

## 関連ノート

{rel}
"""


def render_status_block(statuses: list, runs: list, pending: int, open_reviews: int) -> str:
    lines = ["## 情報源ごとの状態", "", "| 情報源 | 状態 | 最終成功 | 最終試行 | 詳細 |", "| --- | --- | --- | --- | --- |"]
    for s in statuses:
        mark = "OK" if s["status"] == "ok" else "取得できず（不明）"
        lines.append(f"| {s['source']} | {mark} | {s['last_success_at'] or '—'} | {s['last_attempt_at'] or '—'} | {(s['detail'] or '').replace('|', '/')} |")
    lines += ["", "## 直近の実行", ""]
    for r in runs:
        lines.append(f"- {r['started_at']} {r['mode']}: {r['status']}")
    lines += ["", f"- 書込待ち: {pending} 件", f"- 要確認: {open_reviews} 件", ""]
    return "\n".join(lines) + "\n"


def render_review_block(reviews: list) -> str:
    if not reviews:
        return "要確認の項目はありません。\n"
    lines = ["| ID | 種別 | 内容 | 登録 |", "| --- | --- | --- | --- |"]
    for r in reviews:
        lines.append(f"| `{r['review_id']}` | {r['kind']} | {r['detail'].replace('|', '/')} | {r['created_at']} |")
    lines.append("")
    lines.append("解決したら `university-sync review --resolve <ID>` で閉じる。")
    return "\n".join(lines) + "\n"
