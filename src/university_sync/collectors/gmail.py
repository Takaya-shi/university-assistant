"""学校メール（Gmail API）の Collector（Phase 3）。読み取り専用。

gmail.readonly はメールボックス全体を読む権限。検索条件での絞り込みはプログラム側の制限であり、
権限そのものを狭めるものではない（設計書 6 章）。既読化・削除・送信はしない。

差分取得は history ID を使う。期限切れ（404）のときは設定した範囲で取り直し、message ID で二重処理を防ぐ。
"""

from __future__ import annotations

import base64

from . import CollectorError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
RETRIES = 3


class HistoryExpired(CollectorError):
    pass


class GoogleGmailCollector:
    def __init__(self, cfg):
        from googleapiclient.discovery import build

        from ..auth import load_credentials
        from .classroom import SCOPES as CLASSROOM_SCOPES

        creds, self.account_id, self.email = load_credentials(cfg, "school", CLASSROOM_SCOPES + SCOPES)
        self.users = build("gmail", "v1", credentials=creds, cache_discovery=False).users()

    def _exec(self, request, what: str) -> dict:
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=RETRIES)
        except HttpError as e:
            status = e.resp.status
            if status == 404 and what.startswith("history"):
                raise HistoryExpired(f"{what}: history ID が期限切れ", status=404)
            raise CollectorError(f"{what}: HTTP {status} {e.reason}", permanent=status in (400, 401, 403), status=status)
        except OSError as e:
            raise CollectorError(f"{what}: 通信エラー {e}")

    def current_history_id(self) -> str:
        return str(self._exec(self.users.getProfile(userId="me"), "getProfile")["historyId"])

    def search_ids(self, query: str) -> list[str]:
        ids, token = [], None
        while True:
            r = self._exec(self.users.messages().list(userId="me", q=query, maxResults=500, pageToken=token), "messages.list")
            ids += [m["id"] for m in r.get("messages", [])]
            token = r.get("nextPageToken")
            if not token:
                return ids

    def added_since(self, history_id: str) -> tuple[list[str], str]:
        """history.list で追加されたメッセージ ID と、新しい history ID を返す。"""
        ids, token, latest = [], None, history_id
        while True:
            r = self._exec(self.users.history().list(userId="me", startHistoryId=history_id, historyTypes=["messageAdded"],
                                                     maxResults=500, pageToken=token), "history.list")
            for h in r.get("history", []):
                ids += [m["message"]["id"] for m in h.get("messagesAdded", [])]
            latest = str(r.get("historyId", latest))
            token = r.get("nextPageToken")
            if not token:
                return list(dict.fromkeys(ids)), latest

    def get(self, message_id: str) -> dict:
        return self._exec(self.users.messages().get(userId="me", id=message_id, format="full"), f"messages.get {message_id}")

    def attachment(self, message_id: str, attachment_id: str) -> bytes:
        r = self._exec(self.users.messages().attachments().get(userId="me", messageId=message_id, id=attachment_id),
                       f"attachments.get {message_id}")
        return base64.urlsafe_b64decode(r["data"])
