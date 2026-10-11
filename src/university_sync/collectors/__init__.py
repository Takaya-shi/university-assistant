"""取得元（Collector）のインターフェース。

Classroom の一覧 API はすべてページングされる。各 Collector は 1 ページ取得だけを実装し、
全ページの走査は `paginate()` に任せる。途中で失敗した場合は CollectorError を投げ、
呼び出し側は「0件」ではなく「取得失敗」として扱う。
"""

from __future__ import annotations

from typing import Callable, Iterator, Protocol


class CollectorError(Exception):
    def __init__(self, message: str, *, permanent: bool = False, status: int | None = None):
        super().__init__(message)
        self.permanent = permanent  # 403 など。無限再試行しない
        self.status = status


class ReauthRequired(CollectorError):
    """非対話実行で OAuth 同意が必要になった。待たずに終了する。"""


Page = tuple[list[dict], str | None]


def paginate(fetch_page: Callable[[str | None], Page], max_pages: int = 1000) -> Iterator[dict]:
    token: str | None = None
    for _ in range(max_pages):
        items, token = fetch_page(token)
        yield from items
        if not token:
            return
    raise CollectorError(f"ページ数が上限 {max_pages} を超えました（ページングの不整合の可能性）")


class ClassroomCollector(Protocol):
    account_id: str

    def courses_page(self, token: str | None) -> Page: ...

    def coursework_page(self, course_id: str, token: str | None) -> Page: ...

    def my_submissions_page(self, course_id: str, token: str | None) -> Page:
        """courses.courseWork.studentSubmissions.list（courseWorkId='-', userId='me'）相当。"""
        ...

    def materials_page(self, course_id: str, token: str | None) -> Page: ...

    def announcements_page(self, course_id: str, token: str | None) -> Page: ...
