"""サンプル取得元。Classroom API と同じ形の JSON をページ単位で返す。

fixtures/<scenario>/
  courses.json                    [ {"courses": [...], "nextPageToken": "..."}, ... ]
  coursework/<course_id>.json     [ {"courseWork": [...], "nextPageToken": ...}, ... ]
  submissions/<course_id>.json    [ {"studentSubmissions": [...]}, ... ]
  materials/<course_id>.json      [ {"courseWorkMaterial": [...]}, ... ]（任意）
  announcements/<course_id>.json  [ {"announcements": [...]}, ... ]（任意）
  failures.json（任意）            {"coursework": ["<course_id>"], "submissions": [...], "materials": [...]}
"""

from __future__ import annotations

import json
from pathlib import Path

from . import CollectorError, Page


class SampleClassroomCollector:
    account_id = "sample-account"

    def __init__(self, root: Path):
        if not root.is_dir():
            raise CollectorError(f"サンプルデータのディレクトリがありません: {root}", permanent=True)
        self.root = root
        f = root / "failures.json"
        self.failures = json.loads(f.read_text()) if f.exists() else {}

    def _page(self, rel: str, key: str, token: str | None) -> Page:
        path = self.root / rel
        if not path.exists():
            return [], None
        pages = json.loads(path.read_text())
        if not pages:
            return [], None
        idx = int(token) if token else 0
        page = pages[idx]
        nxt = str(idx + 1) if page.get("nextPageToken") else None
        return page.get(key, []), nxt

    def _maybe_fail(self, kind: str, course_id: str) -> None:
        if course_id in self.failures.get(kind, []):
            raise CollectorError(f"サンプルの取得失敗: {kind} {course_id}", status=503)

    def courses_page(self, token: str | None) -> Page:
        return self._page("courses.json", "courses", token)

    def coursework_page(self, course_id: str, token: str | None) -> Page:
        self._maybe_fail("coursework", course_id)
        return self._page(f"coursework/{course_id}.json", "courseWork", token)

    def my_submissions_page(self, course_id: str, token: str | None) -> Page:
        self._maybe_fail("submissions", course_id)
        return self._page(f"submissions/{course_id}.json", "studentSubmissions", token)

    def materials_page(self, course_id: str, token: str | None) -> Page:
        self._maybe_fail("materials", course_id)
        return self._page(f"materials/{course_id}.json", "courseWorkMaterial", token)

    def announcements_page(self, course_id: str, token: str | None) -> Page:
        self._maybe_fail("announcements", course_id)
        return self._page(f"announcements/{course_id}.json", "announcements", token)
