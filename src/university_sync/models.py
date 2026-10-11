"""共通スキーマ（設計書 8.1 の最小データモデル）。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def stable_assignment_id(source_key: str) -> str:
    """source_key から決まる固定ID。延期・改題でも変わらない。"""
    return "ua-" + hashlib.sha256(source_key.encode()).hexdigest()[:10]


@dataclass
class SourceRef:
    kind: str  # classroom / gmail
    id: str
    url: str
    fetched_at: str
    evidence: str = ""


@dataclass
class Course:
    course_id: str
    name: str
    section: str = ""
    state: str = ""
    url: str = ""


@dataclass
class Assignment:
    source_key: str
    course_id: str
    coursework_id: str
    title: str
    description: str
    url: str
    work_type: str
    due_at: str | None  # JST の ISO8601
    due_precision: str  # datetime / date / unknown
    due_raw: dict
    source_updated_at: str | None
    materials: list[dict]
    content_hash: str
    source_refs: list[SourceRef] = field(default_factory=list)
    submission_state_raw: str | None = None
    submission_late: bool | None = None
    submission_verified_at: str | None = None

    @property
    def assignment_id(self) -> str:
        return stable_assignment_id(self.source_key)

    @property
    def due_date(self) -> str | None:
        return self.due_at[:10] if self.due_at else None

    @property
    def due_time(self) -> str | None:
        if self.due_at and self.due_precision == "datetime":
            return self.due_at[11:16]
        return None


@dataclass
class SourceResult:
    """一つの情報源の取得結果。失敗と「0件」を区別するため status を持つ。"""

    source: str
    ok: bool
    fetched_at: str
    detail: str = ""
    complete: bool = False  # 全ページ取得できたか
