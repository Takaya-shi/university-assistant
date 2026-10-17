"""設定ファイル（TOML）の読み込みと、未確定項目の洗い出し。

未設定値は推測で埋めない。空のまま残し、`missing_settings()` で一覧にする。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.config/university-sync/config.toml").expanduser()
TEST_VAULT_MARKER = ".university-sync-test-vault"


class ConfigError(Exception):
    pass


@dataclass
class CourseMapping:
    course_id: str
    course_key: str
    name: str
    vault_dir: str  # Vault 相対。例: Areas/University/論理回路学
    term: str = ""


@dataclass
class Config:
    path: Path
    vault_path: Path
    tasks_note: str
    university_root: str
    review_inbox: str
    protected_paths: list[str]
    state_dir: Path
    source_mode: str  # "sample" | "google"
    sample_dir: Path | None
    school_email: str
    school_mail_backend: str
    storage_email: str
    drive_folder_id: str
    client_secrets_path: str
    courses: list[CourseMapping]
    initial_range_days: int | None
    mail_query: str
    sender_domains: list[str]
    exclude_senders: list[str]
    llm_model: str
    daily_time: str
    notify_channel: str
    approaching_days: list[int]
    llm_mode: str
    auto_complete_on_turned_in: bool
    write_enabled: bool
    copy_materials: bool
    calendar_id: str = ""  # 締切を登録する保存先アカウントのカレンダー。空なら登録しない
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def is_test_vault(self) -> bool:
        return (self.vault_path / TEST_VAULT_MARKER).exists()

    def course_by_id(self, course_id: str) -> CourseMapping | None:
        for c in self.courses:
            if c.course_id == course_id:
                return c
        return None


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("UNIVERSITY_SYNC_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise ConfigError(f"設定ファイルがありません: {path}（config.example.toml をコピーして作成）")
    with path.open("rb") as f:
        raw = tomllib.load(f)

    vault = raw.get("vault", {})
    if not vault.get("path"):
        raise ConfigError("vault.path が未設定です")
    state = raw.get("state", {})
    if not state.get("dir"):
        raise ConfigError("state.dir が未設定です")
    source = raw.get("source", {})
    accounts = raw.get("accounts", {})
    school = accounts.get("school", {})
    storage = accounts.get("storage", {})
    google = raw.get("google", {})
    mail = raw.get("mail", {})
    schedule = raw.get("schedule", {})
    notify = raw.get("notify", {})
    llm = raw.get("llm", {})
    sync = raw.get("sync", {})

    base = path.parent
    sample_dir = source.get("sample_dir")
    sample_path = None
    if sample_dir:
        sample_path = _expand(sample_dir)
        if not sample_path.is_absolute():
            sample_path = (base / sample_path).resolve()

    courses = []
    for c in raw.get("courses", []):
        if not c.get("course_id"):
            continue  # 雛形の空行は未設定として扱う
        courses.append(
            CourseMapping(
                course_id=str(c["course_id"]),
                course_key=c.get("course_key") or str(c["course_id"]),
                name=c.get("name", ""),
                vault_dir=c.get("vault_dir", ""),
                term=c.get("term", ""),
            )
        )

    mode = source.get("mode", "sample")
    if mode not in ("sample", "google"):
        raise ConfigError(f"source.mode は sample / google のいずれか: {mode}")

    return Config(
        path=path,
        vault_path=_expand(vault["path"]),
        tasks_note=vault.get("tasks_note", "Areas/タスク管理/大学.md"),
        university_root=vault.get("university_root", "Areas/University"),
        review_inbox=vault.get("review_inbox", "Inbox/大学_自動取込"),
        protected_paths=vault.get("protected_paths", ["Note/", "タスク置き場.md"]),
        state_dir=_expand(state["dir"]),
        source_mode=mode,
        sample_dir=sample_path,
        school_email=school.get("email", ""),
        school_mail_backend=school.get("mail_backend", ""),
        storage_email=storage.get("email", ""),
        drive_folder_id=storage.get("drive_folder_id", ""),
        client_secrets_path=str(_expand(google["client_secrets_path"])) if google.get("client_secrets_path") else "",
        courses=courses,
        initial_range_days=mail.get("initial_range_days"),
        mail_query=mail.get("query", ""),
        sender_domains=[d.lower().lstrip("@") for d in mail.get("sender_domains", [])],
        exclude_senders=[s.lower() for s in mail.get("exclude_senders", [])],
        llm_model=llm.get("model", "sonnet"),
        daily_time=schedule.get("daily_time", ""),
        notify_channel=notify.get("channel", ""),
        approaching_days=notify.get("approaching_days", []),
        llm_mode=llm.get("mode", ""),
        auto_complete_on_turned_in=sync.get("auto_complete_on_turned_in", True),
        write_enabled=sync.get("write_enabled", False),
        copy_materials=storage.get("copy_classroom_materials", False),
        calendar_id=storage.get("deadline_calendar_id", ""),
        raw=raw,
    )


@dataclass
class MissingSetting:
    key: str
    label: str
    when: str  # 確認するタイミング（設計書 2.3）
    blocks: str  # どの Phase を止めるか


def missing_settings(cfg: Config) -> list[MissingSetting]:
    """設計書 2.3「接続前に確定する設定」のうち、まだ空のもの。"""
    out: list[MissingSetting] = []

    def need(cond: bool, key: str, label: str, when: str, blocks: str) -> None:
        if not cond:
            out.append(MissingSetting(key, label, when, blocks))

    need(bool(cfg.client_secrets_path), "google.client_secrets_path",
         "Google Cloud の OAuth クライアント（デスクトップアプリ）JSON の置き場所", "OAuth接続時", "Phase 1")
    need(bool(cfg.school_email), "accounts.school.email",
         "学校アカウントの完全なメールアドレス", "OAuth接続時", "Phase 1")
    need(bool(cfg.school_mail_backend), "accounts.school.mail_backend",
         "学校メールの基盤（gmail か、それ以外か）", "メール連携着手前", "Phase 3")
    need(bool(cfg.storage_email), "accounts.storage.email",
         "保存先 Drive のアカウントの完全なメールアドレス", "Drive接続時", "Phase 3")
    need(bool(cfg.courses), "courses",
         "対象授業の course ID・学期・Vault 保存先の対応表", "授業一覧から選択時", "Phase 1")
    need(cfg.initial_range_days is not None, "mail.initial_range_days",
         "メールの初回取得範囲（日数）", "初回同期前", "Phase 3")
    need(bool(cfg.sender_domains), "mail.sender_domains",
         "取り込むメールの送信元ドメイン", "初回同期前", "Phase 3")
    need(bool(cfg.daily_time), "schedule.daily_time",
         "毎日の取得時刻（初期候補 07:00 JST）", "定期実行登録前", "Phase 4")
    need(bool(cfg.notify_channel), "notify.channel",
         "通知先", "通知実装時", "Phase 4")
    need(bool(cfg.approaching_days), "notify.approaching_days",
         "期限接近の通知間隔（例: 締切の何日前か）", "通知実装時", "Phase 4")
    need(bool(cfg.llm_mode), "llm.mode",
         "Claude 実行方式・認証・利用上限", "LLM連携着手前", "Phase 3")
    for c in cfg.courses:
        need(bool(c.vault_dir), f"courses[{c.course_id}].vault_dir",
             f"授業 {c.course_id} の Vault 保存先", "授業一覧から選択時", "Phase 2")
    return out
