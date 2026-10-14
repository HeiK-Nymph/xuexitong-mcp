"""编排端到端测试（跑在本地 mock 学习通上）—— 这就是"最优路径"的行为验证。

覆盖：cookie 复用 → 过期/失效回落 → env cookie → 账号密码 → 各种失败分类，
并断言两件关键的事：
  1) 需要滑动验证时【绝不】发登录请求（不静默重试）；
  2) 磁盘上的 state 文件里【绝不】出现明文密码。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

import mock_chaoxing as mock  # noqa: E402

import xuexitong.login as L  # noqa: E402
from xuexitong.config import AuthConfig  # noqa: E402
from xuexitong.session import Cookie, save_cookies  # noqa: E402

USER, PWD = mock.USERNAME, mock.PASSWORD


class AuthFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = mock.start()
        cls._saved = (L.PASSPORT, L.SSO, L.MOOC_API, L.PROTECTED_URL)
        L.PASSPORT = L.SSO = L.MOOC_API = cls.base
        L.PROTECTED_URL = cls.base + "/base"
        # mock 跑在 127.0.0.1，而 cookie blob 的默认域是线上用的 .chaoxing.com
        import xuexitong.session as S
        cls._saved_domain = S.DEFAULT_COOKIE_DOMAIN
        S.DEFAULT_COOKIE_DOMAIN = "127.0.0.1"

    @classmethod
    def tearDownClass(cls):
        L.PASSPORT, L.SSO, L.MOOC_API, L.PROTECTED_URL = cls._saved
        import xuexitong.session as S
        S.DEFAULT_COOKIE_DOMAIN = cls._saved_domain
        mock.stop(cls.server)

    def setUp(self):
        mock.STATE.need_vcode = False
        mock.STATE.login_posts = []
        mock.STATE.login_ok = False
        mock.STATE.token = ""
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "cookies.json"

    def cfg(self, **kw) -> AuthConfig:
        base = dict(username=USER, password=PWD, state_path=self.state)
        base.update(kw)
        return AuthConfig(**base)

    def login_posts(self) -> int:
        return len(mock.STATE.login_posts)

    def preset_session(self, *, valid: bool, expires_in: float = 3600) -> None:
        """预置一份磁盘 cookie：valid=True 时把 mock 侧也置为已登录。"""
        mock.STATE.login_ok = True
        mock.STATE.token = "goodtoken"
        value = "goodtoken" if valid else "staletoken"
        save_cookies([Cookie("MOCKSESS", value, domain="127.0.0.1", path="/",
                             expires=time.time() + expires_in)], self.state)

    # ---------------- 最优路径 ----------------

    def test_path1_password_login_then_cookie_reuse(self):
        """首次：账号密码登录并落盘；第二次：直接复用 cookie，零登录请求。"""
        first = L.ensure_session(self.cfg())
        self.assertTrue(first.ok, first.message)
        self.assertEqual(first.source, "password")
        self.assertEqual(first.uid, "999888")
        self.assertEqual(self.login_posts(), 1)
        self.assertTrue(self.state.is_file())

        second = L.ensure_session(self.cfg())
        self.assertTrue(second.ok, second.message)
        self.assertEqual(second.source, "state-cookie")
        self.assertEqual(self.login_posts(), 1)          # ← 关键：没有重登
        if second.client:
            second.client.close()

    def test_path2_expired_cookie_triggers_one_login(self):
        self.preset_session(valid=True, expires_in=-10)   # 已过期
        info = L.ensure_session(self.cfg())
        self.assertTrue(info.ok)
        self.assertEqual(info.source, "password")
        self.assertEqual(self.login_posts(), 1)

    def test_path3_stale_cookie_falls_back_to_password(self):
        self.preset_session(valid=False)                  # 未过期但服务端不认
        info = L.ensure_session(self.cfg())
        self.assertTrue(info.ok)
        self.assertEqual(info.source, "password")
        self.assertEqual(self.login_posts(), 1)

    def test_path4_env_cookie_skips_login_entirely(self):
        mock.STATE.login_ok = True
        mock.STATE.token = "envtoken"
        info = L.ensure_session(self.cfg(cookie_blob="MOCKSESS=envtoken; _uid=999888"))
        self.assertTrue(info.ok, info.message)
        self.assertEqual(info.source, "env-cookie")
        self.assertEqual(self.login_posts(), 0)           # 完全没登录
        self.assertTrue(self.state.is_file())             # 顺手落盘，下次更快

    def test_verify_business_lists_courses(self):
        info = L.ensure_session(self.cfg(), verify_business=True)
        self.assertTrue(info.ok, info.message)
        ok, n = L.probe_business(info.client)
        self.assertTrue(ok)
        self.assertEqual(n, 1)

    # ---------------- 失败路径 ----------------

    def test_need_vcode_stops_before_any_login_post(self):
        mock.STATE.need_vcode = True
        info = L.ensure_session(self.cfg())
        self.assertFalse(info.ok)
        self.assertEqual(info.error_code, "NEED_HUMAN_VERIFICATION")
        self.assertEqual(self.login_posts(), 0)           # ← 关键：不静默重试
        self.assertFalse(self.state.is_file())

    def test_bad_credentials_propagates_server_message(self):
        info = L.ensure_session(self.cfg(password="wrong-password"))
        self.assertFalse(info.ok)
        self.assertEqual(info.error_code, "BAD_CREDENTIALS")
        self.assertEqual(info.message, "用户名或密码错误")   # 服务端 msg2 原样带出
        self.assertFalse(self.state.is_file())             # 失败不落盘

    def test_no_credential_gives_actionable_error(self):
        info = L.ensure_session(self.cfg(username="", password=""))
        self.assertFalse(info.ok)
        self.assertEqual(info.error_code, "NO_CREDENTIAL")
        self.assertIn("XUEXITONG_COOKIE", info.message)
        self.assertIn("XUEXITONG_USERNAME", info.message)

    def test_password_never_written_to_state_file(self):
        info = L.ensure_session(self.cfg())
        self.assertTrue(info.ok)
        raw = self.state.read_text(encoding="utf-8")
        self.assertNotIn(PWD, raw)
        self.assertNotIn("password", raw)
        self.assertIn("MOCKSESS", raw)
        self.assertIn("expires", json.loads(raw)["cookies"][0])

    def test_service_verifies_credentials_are_aes_encrypted(self):
        """断言真正发出去的是 AES 密文，而不是明文（协议正确性的守门测试）。"""
        info = L.ensure_session(self.cfg())
        self.assertTrue(info.ok)
        form = mock.STATE.login_posts[0]
        self.assertNotEqual(form["uname"], USER)
        self.assertNotEqual(form["password"], PWD)
        from xuexitong.crypto import decrypt
        self.assertEqual(decrypt(form["uname"]), USER)
        self.assertEqual(decrypt(form["password"]), PWD)
        self.assertEqual(form["t"], "true")
        self.assertEqual(form["independentNameId"], "0")   # 官方字段，参考实现漏了
        self.assertEqual(set(form), {
            "fid", "uname", "password", "refer", "t", "forbidotherlogin",
            "validate", "doubleFactorLogin", "independentId", "independentNameId"})


    # ---------------- 探测判据本身 ----------------

    def test_is_login_location_matches_real_world_redirect(self):
        """钉住线上实测的 302 Location 形态（含绝对 URL 与相对路径两种）。"""
        from xuexitong.login import is_login_location
        real = ("https://passport2.chaoxing.com/login?fid=12"
                "&refer=http%3A%2F%2Fi.chaoxing.com%2Fbase%3Fnull&space=2")
        self.assertTrue(is_login_location(real))
        self.assertTrue(is_login_location("/login?fid=0&refer=x"))
        self.assertFalse(is_login_location(""))
        self.assertFalse(is_login_location("https://i.chaoxing.com/base"))

    def test_probe_reports_invalid_when_redirected(self):
        from xuexitong.session import build_client
        client = build_client()
        try:
            valid, uid, name, detail = L.probe(client)
            self.assertFalse(valid)
            self.assertEqual(uid, "")
            self.assertEqual(detail.get("reason"), "redirected-to-login")
        finally:
            client.close()

if __name__ == "__main__":
    unittest.main(verbosity=2)
