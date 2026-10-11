"""保存先 Drive（個人アカウント）への書き込み（設計書 4.1）。

- 学校アカウントとは別の資格情報（storage）を使う。学校の認証で個人 Drive に書かない。
- スコープは drive.file。アプリ自身が作ったファイル・フォルダだけを扱える。
  そのため保存先「大学資料」フォルダもアプリが作る（既存の手動フォルダには書けない）。
- 対象はメール添付と、Classroom の Drive 資料（学校の Drive から読み、こちらへコピー）。
  保存コピーには元のメール ID・元ファイル ID・元の更新日時を説明に残す。
- 学校の資料は、ダウンロードできないもの（先生がコピー・ダウンロードを禁止）は写さない。
"""

from __future__ import annotations

import io

from .collectors import CollectorError

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_NAME = "大学資料"


class DriveStore:
    def __init__(self, cfg, state):
        from googleapiclient.discovery import build

        from .auth import load_credentials

        creds, self.account_id, self.email = load_credentials(cfg, "storage", SCOPES)
        self.files = build("drive", "v3", credentials=creds, cache_discovery=False).files()
        self.state = state
        self.configured_root = cfg.drive_folder_id

    def _exec(self, request, what: str) -> dict:
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=3)
        except HttpError as e:
            raise CollectorError(f"Drive {what}: HTTP {e.resp.status} {e.reason}", permanent=e.resp.status in (400, 401, 403, 404),
                                 status=e.resp.status)

    def _folder(self, name: str, parent: str | None, key: str) -> str:
        cached = self.state.kv_get(key)
        if cached:
            return cached
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            body["parents"] = [parent]
        fid = self._exec(self.files.create(body=body, fields="id"), f"フォルダ作成 {name}")["id"]
        self.state.kv_set(key, fid)
        return fid

    def folder_for(self, year: str, term: str, course: str, sub: str = "メール添付") -> str:
        root = self.configured_root or self._folder(ROOT_NAME, None, "drive:root")
        y = self._folder(year, root, f"drive:{year}")
        t = self._folder(term, y, f"drive:{year}/{term}")
        c = self._folder(course, t, f"drive:{year}/{term}/{course}")
        return self._folder(sub, c, f"drive:{year}/{term}/{course}/{sub}")

    def upload(self, folder_id: str, filename: str, mime: str, data: bytes, description: str,
               target_mime: str | None = None) -> dict:
        """target_mime に Google ドキュメント等の形式を渡すと、変換してネイティブ形式で保存する。"""
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime or "application/octet-stream", resumable=True)
        body = {"name": filename, "parents": [folder_id], "description": description}
        if target_mime:
            body["mimeType"] = target_mime
        return self._exec(self.files.create(body=body, media_body=media, fields="id,webViewLink"), f"アップロード {filename}")



READ_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
# Google ドキュメント等は Office 形式で書き出し、保存先でネイティブ形式に戻す（全部を PDF にしない）
EXPORTS = {
    "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.google-apps.drawing": "application/pdf",
}
NATIVE_TARGET = {k: k for k in EXPORTS if k != "application/vnd.google-apps.drawing"}


class SchoolDriveReader:
    """学校アカウントで Classroom の Drive 資料を読む（読み取り専用）。"""

    def __init__(self, cfg):
        from googleapiclient.discovery import build

        from .auth import load_credentials
        from .collectors.classroom import SCOPES as CLASSROOM_SCOPES

        creds, self.account_id, self.email = load_credentials(cfg, "school", CLASSROOM_SCOPES + READ_SCOPES)
        self.files = build("drive", "v3", credentials=creds, cache_discovery=False).files()

    def _exec(self, request, what: str):
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=3)
        except HttpError as e:
            raise CollectorError(f"学校 Drive {what}: HTTP {e.resp.status} {e.reason}",
                                 permanent=e.resp.status in (400, 401, 403, 404), status=e.resp.status)

    def meta(self, file_id: str) -> dict:
        return self._exec(self.files.get(fileId=file_id, supportsAllDrives=True,
                                         fields="id,name,mimeType,modifiedTime,size,capabilities(canDownload),"
                                                "copyRequiresWriterPermission"), f"情報取得 {file_id}")

    def content(self, meta: dict) -> tuple[bytes, str, str | None]:
        """(中身, 中身の形式, 保存先で変換する形式) を返す。"""
        mime = meta["mimeType"]
        if mime in EXPORTS:
            data = self._exec(self.files.export(fileId=meta["id"], mimeType=EXPORTS[mime]), f"書き出し {meta['name']}")
            return data, EXPORTS[mime], NATIVE_TARGET.get(mime)
        data = self._exec(self.files.get_media(fileId=meta["id"], supportsAllDrives=True), f"ダウンロード {meta['name']}")
        return data, mime, None
