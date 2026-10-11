"""Classroom の Drive 資料を保存先 Drive（個人アカウント）へコピーする（設計書 4.1、Phase 5）。

- 読むのは学校アカウント（drive.readonly）、書くのは保存先アカウント（drive.file）。認証を混ぜない。
- 先生がダウンロード・コピーを禁止している資料は写さず「コピー不可」として記録する。
- 元の資料が更新されたら、新しいコピーを作って一覧のリンクを差し替える（古いコピーは残す）。
- 毎日すべてを写し直さない。元の更新日時が変わったものだけ写す。
- dry-run では何も写さず、写す予定の件数だけ数える。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import resources
from .collectors import CollectorError, paginate
from .timeutil import iso_now

MAX_BYTES = 100 * 1024 * 1024


@dataclass
class CopyReport:
    ok: bool = True
    detail: str = ""
    copied: int = 0
    planned: int = 0
    restricted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _targets(cfg, collector) -> list[tuple]:
    """(授業, 元ファイル ID, 題名) の一覧。資料・お知らせ・課題に付いた Drive ファイルが対象。"""
    out, seen = [], set()
    for course in cfg.courses:
        if not course.vault_dir:
            continue
        works = list(paginate(lambda t: collector.coursework_page(course.course_id, t)))
        rows, _ = resources.collect(collector, course.course_id, works)
        for r in rows:
            m = r["material"]
            if m["kind"] == "drive" and m.get("id") and m["id"] not in seen:
                seen.add(m["id"])
                out.append((course, m["id"], m.get("title", "")))
    return out


def _folder_names(course) -> tuple[str, str]:
    m = re.match(r"(\d{4})(?:年度)?(.*)", course.term or "")
    return (f"{m.group(1)}年度", m.group(2) or "未分類") if m else ("未分類", "未分類")


def copy_materials(cfg, store, collector, reader, drive, *, dry_run: bool) -> CopyReport:
    report = CopyReport()
    try:
        targets = _targets(cfg, collector)
    except CollectorError as e:
        report.ok, report.detail = False, f"資料一覧の取得に失敗: {e}"
        return report
    for course, source_id, title in targets:
        prev = store.material_copy(source_id)
        try:
            meta = reader.meta(source_id)
        except CollectorError as e:
            if not dry_run:
                store.set_material_copy(source_id, course_id=course.course_id, name=title, status="failed",
                                        detail=str(e)[:300])
            report.errors.append(f"{title}: {e}")
            continue
        unchanged = prev is not None and prev["source_modified"] == meta.get("modifiedTime")
        if unchanged and prev["status"] in ("saved", "restricted"):
            continue
        if not meta.get("capabilities", {}).get("canDownload", False) or meta.get("copyRequiresWriterPermission"):
            report.restricted.append(meta.get("name", title))
            if not dry_run:
                store.set_material_copy(source_id, course_id=course.course_id, name=meta.get("name"), source_mime=meta.get("mimeType"),
                                        source_modified=meta.get("modifiedTime"), status="restricted",
                                        detail="先生がダウンロード・コピーを許可していない", copy_id=prev["copy_id"] if prev else None,
                                        link=prev["link"] if prev else None)
            continue
        if int(meta.get("size") or 0) > MAX_BYTES:
            report.errors.append(f"{meta['name']}: {MAX_BYTES // 1024 // 1024}MB を超えるため写さない")
            continue
        report.planned += 1
        if dry_run:
            continue
        try:
            data, mime, target = reader.content(meta)
            year, term = _folder_names(course)
            folder = drive.folder_for(year, term, course.name, "授業資料")
            desc = (f"Classroom の資料のコピー。元ファイル {source_id} / 元の更新 {meta.get('modifiedTime')} / "
                    f"取得 {iso_now()} / 形式 {meta.get('mimeType')}")
            f = drive.upload(folder, meta["name"], mime, data, desc, target_mime=target)
            store.set_material_copy(source_id, course_id=course.course_id, name=meta["name"], source_mime=meta["mimeType"],
                                    source_modified=meta.get("modifiedTime"), copy_id=f["id"], link=f.get("webViewLink"),
                                    status="saved", detail="", copied_at=iso_now())
            report.copied += 1
        except CollectorError as e:
            store.set_material_copy(source_id, course_id=course.course_id, name=meta.get("name"), status="failed", detail=str(e)[:300])
            report.errors.append(f"{meta.get('name', title)}: {e}")
    if report.errors:
        report.ok = False
        report.detail = f"{len(report.errors)} 件を写せなかった（次回再試行）"
    return report
