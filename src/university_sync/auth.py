"""Google OAuth（設計書 6 章）。学校用と保存先用で資格情報を分け、macOS キーチェーンに置く。

- 認可（ブラウザでの同意）は `university-sync auth <role>` の対話実行だけで行う。
- 同期などの非対話実行では保存済みトークンの更新だけを試み、だめなら ReauthRequired で止まる。
- 認可したアカウントのメールアドレスが設定と違えば保存しない（別アカウントでの取り違え防止）。
- トークンはログ・Vault・リポジトリに出さない。
"""

from __future__ import annotations

import base64
import json
import os

from .collectors import ReauthRequired
from .config import Config, ConfigError

KEYRING_SERVICE = "university-sync"
IDENTITY_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
_G = "https://www.googleapis.com/auth/"
# Google がトークン応答で別名に置き換えて返すスコープ。同じ権限として扱う
SCOPE_ALIASES = {
    _G + "classroom.coursework.me.readonly": _G + "classroom.student-submissions.me.readonly",
}


def canonical(scopes) -> set[str]:
    return {SCOPE_ALIASES.get(s, s) for s in scopes or []}


def _keyring():
    import keyring
    return keyring


def _role_email(cfg: Config, role: str) -> str:
    email = {"school": cfg.school_email, "storage": cfg.storage_email}.get(role)
    if email is None:
        raise ConfigError(f"不明なアカウント種別: {role}（school / storage）")
    if not email:
        raise ConfigError(f"accounts.{role}.email が未設定です。推測では認可しない")
    return email


def _id_claims(id_token: str | None) -> dict:
    """Google のトークン応答で直接受け取った ID トークンから email と sub を読む。"""
    if not id_token:
        return {}
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def authorize(cfg: Config, role: str, scopes: list[str]) -> dict:
    """ブラウザで同意を取り、確認したアカウントの資格情報をキーチェーンに保存する。"""
    from google_auth_oauthlib.flow import InstalledAppFlow

    email = _role_email(cfg, role)
    if not cfg.client_secrets_path:
        raise ConfigError("google.client_secrets_path が未設定です")
    # 応答のスコープ名が別名に変わるだけで止まらないようにする。許可範囲は下で自分で照合する
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow = InstalledAppFlow.from_client_secrets_file(cfg.client_secrets_path, scopes=scopes + IDENTITY_SCOPES)
    creds = flow.run_local_server(port=0, login_hint=email, prompt="consent", open_browser=True,
                                  authorization_prompt_message="ブラウザで認可画面を開きます: {url}",
                                  success_message="認可が完了しました。このタブは閉じてかまいません。")
    claims = _id_claims(creds.id_token)
    actual = claims.get("email", "")
    if actual.lower() != email.lower():
        raise ConfigError(f"認可したアカウント（{actual or '不明'}）が設定（{email}）と違うため保存しない")
    granted_attr = getattr(creds, "granted_scopes", None)
    granted = list(granted_attr if granted_attr is not None else (creds.scopes or []))
    missing = canonical(scopes) - canonical(granted)
    if missing:
        raise ConfigError("許可されなかった権限があるため保存しない: " + ", ".join(sorted(missing))
                          + "（認可画面ですべての項目にチェックを入れて再実行）")
    info = json.loads(creds.to_json())
    info["scopes"] = granted  # 更新時に「要求したが付与されていない」と判定されないよう、付与された名前で持つ
    record = {"email": actual, "account_id": claims.get("sub", ""), "credentials": info}
    _keyring().set_password(KEYRING_SERVICE, f"{role}:{email}", json.dumps(record))
    return {"email": actual, "account_id": record["account_id"], "scopes": sorted(granted)}


def load_credentials(cfg: Config, role: str, scopes: list[str]):
    """非対話で資格情報を取り出す。更新できなければ ReauthRequired。"""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    email = _role_email(cfg, role)
    raw = _keyring().get_password(KEYRING_SERVICE, f"{role}:{email}")
    if not raw:
        raise ReauthRequired(f"{role} アカウントが未認可です。`university-sync auth {role}` を実行", permanent=True)
    record = json.loads(raw)
    creds = Credentials.from_authorized_user_info(record["credentials"])
    missing = canonical(scopes) - canonical(creds.scopes)
    if missing:
        raise ReauthRequired(f"追加の権限が必要です（{', '.join(sorted(missing))}）。`university-sync auth {role}` を再実行", permanent=True)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise ReauthRequired(f"{role} アカウントの再認可が必要です（{e.__class__.__name__}）", permanent=True)
        record["credentials"] = json.loads(creds.to_json())
        _keyring().set_password(KEYRING_SERVICE, f"{role}:{email}", json.dumps(record))
    return creds, record["account_id"], record["email"]


def forget(cfg: Config, role: str) -> bool:
    email = _role_email(cfg, role)
    k = _keyring()
    if not k.get_password(KEYRING_SERVICE, f"{role}:{email}"):
        return False
    k.delete_password(KEYRING_SERVICE, f"{role}:{email}")
    return True
