"""Classroom API の生レスポンスを共通スキーマへ変換する。"""

from __future__ import annotations

import hashlib
import json

from .models import Assignment, Course, SourceRef
from .timeutil import classroom_due_to_jst


def content_fingerprint(work: dict) -> str:
    """意味のある変更だけを拾う指紋。updateTime や閲覧状態は含めない。"""
    meaningful = {
        "title": work.get("title", ""),
        "description": work.get("description", ""),
        "dueDate": work.get("dueDate"),
        "dueTime": work.get("dueTime"),
        "workType": work.get("workType"),
        "maxPoints": work.get("maxPoints"),
        # 生の materials には取得のたびに変わる thumbnailUrl が入るため、要約だけを使う
        "materials": [_material_summary(m) for m in work.get("materials", [])],
        "state": work.get("state"),
    }
    blob = json.dumps(meaningful, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def normalize_course(raw: dict) -> Course:
    return Course(
        course_id=str(raw["id"]),
        name=raw.get("name", ""),
        section=raw.get("section", ""),
        state=raw.get("courseState", ""),
        url=raw.get("alternateLink", ""),
    )


def _material_summary(m: dict) -> dict:
    if "driveFile" in m:
        f = m["driveFile"].get("driveFile", {})
        return {"kind": "drive", "id": f.get("id", ""), "title": f.get("title", ""), "url": f.get("alternateLink", "")}
    if "link" in m:
        return {"kind": "link", "title": m["link"].get("title", ""), "url": m["link"].get("url", "")}
    if "youtubeVideo" in m:
        v = m["youtubeVideo"]
        return {"kind": "youtube", "title": v.get("title", ""), "url": v.get("alternateLink", "")}
    if "form" in m:
        return {"kind": "form", "title": m["form"].get("title", ""), "url": m["form"].get("formUrl", "")}
    return {"kind": "unknown", "title": "", "url": ""}


def normalize_coursework(
    account_id: str, course_id: str, work: dict, submission: dict | None, fetched_at: str
) -> Assignment:
    due_at, precision = classroom_due_to_jst(work.get("dueDate"), work.get("dueTime"))
    source_key = f"classroom:{account_id}:{course_id}:{work['id']}"
    url = work.get("alternateLink", "")
    a = Assignment(
        source_key=source_key,
        course_id=course_id,
        coursework_id=str(work["id"]),
        # 課題名に改行が入っていることがある。タスク行が割れないよう空白をまとめる
        title=" ".join(work.get("title", "").split()) or "(無題)",
        description=work.get("description", ""),
        url=url,
        work_type=work.get("workType", ""),
        due_at=due_at,
        due_precision=precision,
        due_raw={"dueDate": work.get("dueDate"), "dueTime": work.get("dueTime")},
        source_updated_at=work.get("updateTime"),
        materials=[_material_summary(m) for m in work.get("materials", [])],
        content_hash=content_fingerprint(work),
        source_refs=[SourceRef("classroom", str(work["id"]), url, fetched_at)],
    )
    if submission is not None:
        a.submission_state_raw = submission.get("state")
        a.submission_late = submission.get("late")
        a.submission_verified_at = fetched_at
    return a
