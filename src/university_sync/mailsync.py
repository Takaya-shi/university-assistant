"""学校メールの同期（Phase 3）。

1. 新着メッセージ ID を取得し、先に状態ストアへ保存してから history ID を進める（取りこぼし防止）。
2. 対象ドメイン以外・自分の送信は無視（件名も保存しない）。
3. Classroom の通知メールは本文のリンクから既存の課題に紐付けるだけ。Claude には渡さない。
4. 大学ドメインのメールは Claude で候補を抽出し、検証して「要確認の候補」にする（自動登録しない）。
5. 添付は保存先 Drive（個人アカウント）へ保存する。
dry-run は状態・Drive を変えず、Claude も呼ばない。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from .collectors import CollectorError
from .collectors.gmail import HistoryExpired
from .llm import LLMError, validate
from .mail import Mail, classroom_refs, gmail_link, parse
from .timeutil import now_jst

MAX_ATTEMPTS = 3
CLASSROOM_DOMAIN = "classroom.google.com"
CURSOR_KEY = "gmail:history_id"


@dataclass
class MailReport:
    ok: bool = True
    detail: str = ""
    new_ids: int = 0
    relevant: int = 0
    classroom_linked: int = 0
    llm_calls: int = 0
    llm_planned: int = 0
    candidates: list[str] = field(default_factory=list)
    attachments_saved: int = 0
    attachment_errors: list[str] = field(default_factory=list)
    retry_waiting: int = 0
    reviews: list[tuple] = field(default_factory=list)
    notifications: list[tuple] = field(default_factory=list)


def domain_matches(domain: str, domains: list[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def build_query(cfg, days: int) -> str:
    froms = " OR ".join(cfg.sender_domains)
    return f"from:({froms}) -in:sent -in:chats newer_than:{days}d"


def candidate_id(message_id: str, idx: int) -> str:
    return "mc-" + hashlib.sha256(f"{message_id}:{idx}".encode()).hexdigest()[:8]


def _fetch_ids(cfg, store, gmail, report: MailReport) -> tuple[list[str], str | None]:
    cursor = store.kv_get(CURSOR_KEY)
    if cursor:
        try:
            return gmail.added_since(cursor)
        except HistoryExpired:
            report.detail = "history ID が期限切れのため、設定範囲で取り直した（message ID で重複を除外）"
    latest = gmail.current_history_id()  # 検索より先に控え、検索中に届いたメールを次回拾えるようにする
    return gmail.search_ids(build_query(cfg, fallback_days(cfg, store))), latest


def fallback_days(cfg, store) -> int:
    """取り直す日数。Mac が長く止まっていたら、最後に成功した日までさかのぼる（1 日の重複は ID で除く）。"""
    days = cfg.initial_range_days or 0
    row = next((s for s in store.source_statuses() if s["source"] == "gmail"), None)
    if row and row["last_success_at"]:
        from datetime import datetime
        gap = (now_jst() - datetime.fromisoformat(row["last_success_at"])).days + 1
        days = max(days, gap)
    return days


def _classify(cfg, mail: Mail, own_email: str) -> str:
    if "SENT" in mail.labels or "CHAT" in mail.labels or mail.sender == own_email.lower():
        return "ignored"
    if not domain_matches(mail.sender_domain, cfg.sender_domains) or mail.sender in cfg.exclude_senders:
        return "ignored"
    return "classroom" if domain_matches(mail.sender_domain, [CLASSROOM_DOMAIN]) else "university"


def sync_mail(cfg, store, gmail, llm, drive, *, dry_run: bool) -> MailReport:
    report = MailReport()
    if cfg.initial_range_days is None or not cfg.sender_domains:
        report.ok, report.detail = False, "mail.initial_range_days / mail.sender_domains が未設定"
        return report
    now = now_jst().isoformat(timespec="seconds")
    try:
        ids, latest = _fetch_ids(cfg, store, gmail, report)
        fresh = [i for i in ids if store.mail(i) is None]
        report.new_ids = len(fresh)
        if dry_run:
            for mid in fresh:
                kind = _classify(cfg, parse(gmail.get(mid)), gmail.email)
                report.relevant += kind != "ignored"
                report.llm_planned += kind == "university" and llm is not None
            return report
        store.add_mail_ids(fresh, now)
        if latest:
            store.kv_set(CURSOR_KEY, latest)
        store.kv_set("gmail:account_id", gmail.account_id)
    except CollectorError as e:
        report.ok, report.detail = False, str(e)
        return report

    names = [c.name for c in cfg.courses]
    for row in store.mails_to_process(MAX_ATTEMPTS):
        try:
            _process(cfg, store, gmail, llm, drive, row, names, now, report)
        except CollectorError as e:
            attempts = row["attempts"] + 1
            store.update_mail(row["message_id"], status="pending" if attempts < MAX_ATTEMPTS else "failed",
                              attempts=attempts, error=str(e)[:300])
            report.ok, report.detail = False, str(e)
    if drive is not None:
        _retry_attachments(cfg, store, gmail, drive, report)
    report.retry_waiting = len(store.mails_to_process(MAX_ATTEMPTS))
    return report


def _retry_attachments(cfg, store, gmail, drive, report: MailReport) -> None:
    """前回 Drive 未接続・失敗で保存できなかった添付をもう一度保存する。"""
    rows = store.conn.execute("SELECT DISTINCT message_id FROM mail_attachments WHERE status IN ('failed','skipped')").fetchall()
    for r in rows:
        try:
            mail = parse(gmail.get(r["message_id"]))
        except CollectorError as e:
            report.attachment_errors.append(str(e))
            continue
        cands = [json.loads(c["data"]) for c in store.conn.execute(
            "SELECT data FROM mail_candidates WHERE message_id=?", (r["message_id"],)).fetchall()]
        _save_attachments(cfg, store, gmail, drive, mail, cands, report)


def _process(cfg, store, gmail, llm, drive, row, names, now, report: MailReport) -> None:
    mid = row["message_id"]
    mail = parse(gmail.get(mid))
    kind = _classify(cfg, mail, gmail.email)
    if kind == "ignored":
        store.update_mail(mid, kind="ignored", status="ignored", processed_at=now)  # 件名・差出人は残さない
        return
    report.relevant += 1
    store.update_mail(mid, thread_id=mail.thread_id, received_at=mail.received_at, sender=mail.sender,
                      subject=mail.subject, kind=kind)

    linked = []
    for course_id, item_id in classroom_refs(mail.body):
        a = store.assignment_by_source(f"classroom:{gmail.account_id}:{course_id}:{item_id}")
        if a:
            store.link_mail(mid, a["assignment_id"])
            linked.append(a)
    if kind == "classroom":
        # Classroom 側の課題は API で取得済み。通知メールは紐付けだけにして二重登録しない
        report.classroom_linked += len(linked)
        store.update_mail(mid, status="done", processed_at=now)
        return

    if llm is None:
        cands = [{"event_type": "other", "title": mail.subject, "course_candidates": [], "deadline_candidates": [],
                  "evidence_text": "", "needs_review": True, "review_reason": "Claude を使わない設定のため未解析"}]
    else:
        report.llm_calls += 1
        try:
            cands = validate(llm.extract(mail, names), mail, names)
        except LLMError as e:
            attempts = row["attempts"] + 1
            store.update_mail(mid, status="pending" if attempts < MAX_ATTEMPTS else "failed", attempts=attempts, error=str(e)[:300])
            if attempts >= MAX_ATTEMPTS:
                report.reviews.append((f"mail_failed:{mid}", "メールの解析に失敗", None,
                                       f"「{mail.subject}」（{mail.received_at}）を {MAX_ATTEMPTS} 回解析できなかった。Gmail で直接確認"))
            return

    for idx, c in enumerate(cands):
        c["linked_assignments"] = [a["assignment_id"] for a in linked]
        for a in linked:
            for d in c["deadline_candidates"]:
                if a["due_at"] and d["date"] != a["due_at"][:10]:
                    c["review_reason"] = "、".join(filter(None, [c["review_reason"], f"Classroom の期限（{a['due_at'][:16]}）と違う"]))
                    report.reviews.append((f"due_conflict:{a['assignment_id']}:{d['date']}", "期限の矛盾", a["assignment_id"],
                                           f"{a['title']}: Classroom は {a['due_at'][:16]}、メール「{mail.subject}」は「{d['raw']}」。どちらも残してある"))
        cid = candidate_id(mid, idx)
        if store.add_candidate(cid, mid, idx, c, now):
            report.candidates.append(cid)
            report.notifications.append((f"mail_candidate:{cid}", "mail_candidate", None, f"メールから候補: {c['title']}（要確認）"))

    _save_attachments(cfg, store, gmail, drive, mail, cands, report)
    store.update_mail(mid, status="done", processed_at=now, error=None)


def _save_attachments(cfg, store, gmail, drive, mail: Mail, cands, report: MailReport) -> None:
    if not mail.attachments:
        return
    done = {r["filename"] for r in store.attachments_for(mail.message_id) if r["status"] == "saved"}
    if drive is None:
        for att in mail.attachments:
            if att.filename not in done:
                store.set_attachment(mail.message_id, att.filename, "skipped", detail="保存先 Drive が未接続")
        return
    course = next((c for c in cfg.courses if any(c.name in x["course_candidates"] for x in cands)), None)
    m = re.match(r"(\d{4})(?:年度)?(.*)", course.term if course else "")
    year, half = (f"{m.group(1)}年度", m.group(2) or "未分類") if m else ("未分類", "未分類")
    try:
        folder = drive.folder_for(year, half, course.name if course else "未分類")
    except CollectorError as e:
        report.attachment_errors.append(str(e))
        return
    for att in mail.attachments:
        if att.filename in done:
            continue
        try:
            data = gmail.attachment(mail.message_id, att.attachment_id)
            desc = f"学校メールの添付。受信 {mail.received_at} / 差出人 {mail.sender} / 件名 {mail.subject} / message {mail.message_id}"
            f = drive.upload(folder, att.filename, att.mime_type, data, desc)
            store.set_attachment(mail.message_id, att.filename, "saved", f["id"], f.get("webViewLink"))
            report.attachments_saved += 1
        except CollectorError as e:
            store.set_attachment(mail.message_id, att.filename, "failed", detail=str(e)[:300])
            report.attachment_errors.append(f"{att.filename}: {e}")


def render_candidates_block(store, school_email: str) -> str:
    rows = store.open_candidates()
    if not rows:
        return "未確認の候補はありません。\n"
    labels = {"assignment": "課題", "deadline_change": "期限変更", "cancellation": "休講・中止", "exam": "試験",
              "info": "連絡", "other": "その他"}
    from .vault import sanitize_external
    lines = []
    for r in rows:
        c = json.loads(r["data"])
        lines += [f"### `{r['candidate_id']}` 〔{labels.get(c['event_type'], c['event_type'])}〕 {c['title']}", ""]
        lines.append(f"- 授業の候補: {'、'.join(c['course_candidates']) or '不明'}")
        if c["deadline_candidates"]:
            ds = [f"{d['date']} {d['time']}".strip() + f"（本文「{d['raw']}」）" for d in c["deadline_candidates"]]
            lines.append(f"- 期限の候補: {' / '.join(ds)}")
        else:
            lines.append("- 期限の候補: なし（日付の記載なし、または確定できない）")
        if c.get("review_reason"):
            lines.append(f"- 確認してほしい点: {c['review_reason']}")
        lines.append(f"- メール: {r['received_at'][:16]} {r['sender']}「{r['subject']}」 [Gmail で開く]({gmail_link(school_email, r['message_id'])})")
        atts = store.attachments_for(r["message_id"])
        if atts:
            parts = [f"[{a['filename']}]({a['link']})" if a["link"] else f"{a['filename']}（{a['status']}: {a['detail'] or ''}）" for a in atts]
            lines.append(f"- 添付（Drive 保存）: {'、'.join(parts)}")
        if c.get("evidence_text"):
            lines += ["", sanitize_external(c["evidence_text"])]
        lines.append("")
    lines += ["課題として登録: `university-sync review --accept <候補ID>`（期限・授業は `--due` `--course` で指定可）",
              "不要: `university-sync review --dismiss <候補ID>`", ""]
    return "\n".join(lines)
