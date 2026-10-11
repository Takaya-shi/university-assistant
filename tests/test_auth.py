"""認可まわり。実際の Google・キーチェーンには触れない。"""

from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from university_sync import auth
from university_sync.collectors import ReauthRequired
from university_sync.config import ConfigError


def fake_id_token(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, s, u):
        return self.store.get((s, u))

    def set_password(self, s, u, p):
        self.store[(s, u)] = p

    def delete_password(self, s, u):
        del self.store[(s, u)]


def cfg(**kw):
    base = dict(school_email="k@example.ac.jp", storage_email="", client_secrets_path="/tmp/cs.json")
    return SimpleNamespace(**(base | kw))


class AuthTests(unittest.TestCase):
    def setUp(self):
        # テストから Google のトークン発行先へ通信しない
        net = mock.patch("google.oauth2.credentials.Credentials.refresh", side_effect=AssertionError("network"))
        net.start()
        self.addCleanup(net.stop)
        self.kr = FakeKeyring()
        p = mock.patch.object(auth, "_keyring", return_value=self.kr)
        p.start()
        self.addCleanup(p.stop)

    def _flow(self, email, granted=("s",)):
        creds = mock.Mock(id_token=fake_id_token({"email": email, "sub": "123"}), scopes=["s"], granted_scopes=list(granted))
        creds.to_json.return_value = json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "x",
                                                  "expiry": "2099-01-01T00:00:00Z"})
        flow = mock.Mock()
        flow.run_local_server.return_value = creds
        return mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", return_value=flow)

    def test_saves_when_email_matches(self):
        with self._flow("K@example.ac.jp"):
            info = auth.authorize(cfg(), "school", ["s"])
        self.assertEqual(info["account_id"], "123")
        self.assertIn(("university-sync", "school:k@example.ac.jp"), self.kr.store)

    def test_alias_scope_accepted_and_stored_as_granted(self):
        req = auth._G + "classroom.coursework.me.readonly"
        got = auth._G + "classroom.student-submissions.me.readonly"
        with self._flow("k@example.ac.jp", granted=[got]):
            auth.authorize(cfg(), "school", [req])
        rec = json.loads(self.kr.store[("university-sync", "school:k@example.ac.jp")])
        self.assertEqual(rec["credentials"]["scopes"], [got])
        # 要求名（別名）で読み出しても、追加認可を求めずに通る
        _, account_id, _ = auth.load_credentials(cfg(), "school", [req])
        self.assertEqual(account_id, "123")

    def test_unchecked_scope_refused(self):
        with self._flow("k@example.ac.jp", granted=[]):
            with self.assertRaises(ConfigError):
                auth.authorize(cfg(), "school", ["s"])
        self.assertEqual(self.kr.store, {})

    def test_refuses_other_account(self):
        with self._flow("someone@gmail.com"):
            with self.assertRaises(ConfigError):
                auth.authorize(cfg(), "school", ["s"])
        self.assertEqual(self.kr.store, {})

    def test_requires_email_configured(self):
        with self.assertRaises(ConfigError):
            auth.authorize(cfg(school_email=""), "school", ["s"])

    def test_noninteractive_without_token_stops(self):
        with self.assertRaises(ReauthRequired):
            auth.load_credentials(cfg(), "school", ["s"])

    def test_missing_scope_requires_reauth(self):
        self.kr.store[("university-sync", "school:k@example.ac.jp")] = json.dumps({
            "email": "k@example.ac.jp", "account_id": "123",
            "credentials": {"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "x", "scopes": ["a"]}})
        with self.assertRaises(ReauthRequired):
            auth.load_credentials(cfg(), "school", ["a", "b"])


if __name__ == "__main__":
    unittest.main()
