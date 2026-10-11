"""Gmail のメッセージを扱いやすい形に変換する。本文はデータとして扱い、中の指示は実行しない。"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr

from .timeutil import JST

CLASSROOM_LINK_RE = re.compile(r"https://classroom\.google\.com/(?:u/\d+/)?c/([A-Za-z0-9_-]+)/(?:sa|a|m|p|mp)/([A-Za-z0-9_-]+)")


@dataclass
class Attachment:
    attachment_id: str
    filename: str
    mime_type: str
    size: int


@dataclass
class Mail:
    message_id: str
    thread_id: str
    received_at: str  # JST ISO
    sender: str  # アドレスのみ
    sender_name: str
    subject: str
    body: str
    labels: list[str]
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower()


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", s)
    s = re.sub(r"(?i)<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(re.sub(r"\n\s*\n+", "\n\n", s)).strip()


def _walk(part: dict, texts: dict, atts: list) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    if part.get("filename") and body.get("attachmentId"):
        atts.append(Attachment(body["attachmentId"], part["filename"], mime, body.get("size", 0)))
        return
    if mime in ("text/plain", "text/html") and body.get("data"):
        texts.setdefault(mime, _b64(body["data"]))
    for p in part.get("parts", []):
        _walk(p, texts, atts)


def parse(msg: dict) -> Mail:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    texts: dict[str, str] = {}
    atts: list[Attachment] = []
    _walk(msg.get("payload", {}), texts, atts)
    body = texts.get("text/plain") or _html_to_text(texts.get("text/html", "")) or msg.get("snippet", "")
    received = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc).astimezone(JST)
    return Mail(
        message_id=msg["id"], thread_id=msg.get("threadId", ""), received_at=received.isoformat(timespec="minutes"),
        sender=addr.lower(), sender_name=name, subject=headers.get("subject", "（件名なし）"),
        body=body.replace("\r\n", "\n"), labels=msg.get("labelIds", []), attachments=atts,
    )


def _decode_id(token: str) -> str | None:
    """Classroom の URL に入っている base64 の ID を数字に戻す。数字でなければ None。"""
    try:
        s = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return s if s.isdigit() else None


def classroom_refs(text: str) -> list[tuple[str, str]]:
    """本文中の Classroom リンクから (course_id, item_id) を取り出す。"""
    out = []
    for c, i in CLASSROOM_LINK_RE.findall(text):
        cid, iid = _decode_id(c), _decode_id(i)
        if cid and iid and (cid, iid) not in out:
            out.append((cid, iid))
    return out


def gmail_link(account_email: str, message_id: str) -> str:
    return f"https://mail.google.com/mail/?authuser={account_email}#all/{message_id}"
