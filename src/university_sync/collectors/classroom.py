"""Google Classroom API の Collector（Phase 1）。読み取り専用。

採用スコープ（設計書 6 章）:
  classroom.courses.readonly
  classroom.coursework.me.readonly        課題と本人の提出状態
  classroom.announcements.readonly        お知らせ（資料一覧に載せる）
  classroom.courseworkmaterials.readonly  資料（資料一覧に載せる）
"""

from __future__ import annotations

from ..config import Config, ConfigError
from . import CollectorError, Page

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
]
PAGE_SIZE = 100
RETRIES = 3  # 429・5xx は googleapiclient が指数バックオフで再試行する


class GoogleClassroomCollector:
    def __init__(self, cfg: Config):
        from googleapiclient.discovery import build

        from ..auth import load_credentials

        if not cfg.client_secrets_path or not cfg.school_email:
            raise ConfigError("Classroom 接続には google.client_secrets_path と accounts.school.email が必要です")
        creds, self.account_id, self.email = load_credentials(cfg, "school", SCOPES)
        self.service = build("classroom", "v1", credentials=creds, cache_discovery=False)

    def _exec(self, request, what: str) -> dict:
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=RETRIES)
        except HttpError as e:
            status = e.resp.status
            raise CollectorError(f"{what}: HTTP {status} {e.reason}", permanent=status in (400, 401, 403, 404), status=status)
        except OSError as e:
            raise CollectorError(f"{what}: 通信エラー {e}")

    def courses_page(self, token: str | None) -> Page:
        r = self._exec(self.service.courses().list(studentId="me", pageSize=PAGE_SIZE, pageToken=token), "courses.list")
        return r.get("courses", []), r.get("nextPageToken")

    def coursework_page(self, course_id: str, token: str | None) -> Page:
        r = self._exec(self.service.courses().courseWork().list(courseId=course_id, pageSize=PAGE_SIZE, pageToken=token),
                       f"courseWork.list {course_id}")
        return r.get("courseWork", []), r.get("nextPageToken")

    def my_submissions_page(self, course_id: str, token: str | None) -> Page:
        r = self._exec(self.service.courses().courseWork().studentSubmissions().list(
            courseId=course_id, courseWorkId="-", userId="me", pageSize=PAGE_SIZE, pageToken=token),
            f"studentSubmissions.list {course_id}")
        return r.get("studentSubmissions", []), r.get("nextPageToken")

    def materials_page(self, course_id: str, token: str | None) -> Page:
        r = self._exec(self.service.courses().courseWorkMaterials().list(courseId=course_id, pageSize=PAGE_SIZE, pageToken=token),
                       f"courseWorkMaterials.list {course_id}")
        return r.get("courseWorkMaterial", []), r.get("nextPageToken")

    def announcements_page(self, course_id: str, token: str | None) -> Page:
        r = self._exec(self.service.courses().announcements().list(courseId=course_id, pageSize=PAGE_SIZE, pageToken=token),
                       f"announcements.list {course_id}")
        return r.get("announcements", []), r.get("nextPageToken")
