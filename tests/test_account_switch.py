"""切换账号的回归测试 —— 钉住「旧会话/旧缓存绝不能沿用」这条规则。

背景：实测发现换 .env 账号后，磁盘上旧账号的 cookie 在服务端仍有效，
       探测会通过 → 静默沿用旧会话（whoami 报的仍是旧 uid）。必须靠 account 绑定识别。
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
from xuexitong import store  # noqa: E402
from xuexitong.config import AuthConfig  # noqa: E402
from xuexitong.session import Cookie, load_state, save_cookies  # noqa: E402

USER, PWD = mock.USERNAME, mock.PASSWORD


class StateAccountBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "cookies.json"

    def test_save_and_load_account(self):
        save_cookies([Cookie("_uid", "123")], self.state, account="alice")
        st = load_state(self.state)
        self.assertEqual(st.account, "alice")
        self.assertEqual([c.name for c in st.cookies], ["_uid"])

    def test_load_state_tolerates_legacy_file_without_account(self):
        self.state.write_text(json.dumps({"saved_at": 1, "cookies": [
            {"name": "_uid", "value": "9", "domain": ".chaoxing.com", "path": "/"}]}), encoding="utf-8")
        st = load_state(self.state)
        self.assertEqual(st.account, "")           # 旧文件没有归属信息
        self.assertEqual(len(st.cookies), 1)


class MenuCacheScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "menu_cache.json"

    def test_mismatched_account_invalidates_cache(self):
        store.save_menu_cache({"cid:clazz": {"url": "/work/getAllWork?enc=AAA"}}, "alice", self.cache)
        self.assertEqual(store.load_menu_cache("alice", self.cache),
                         {"cid:clazz": {"url": "/work/getAllWork?enc=AAA"}})
        self.assertEqual(store.load_menu_cache("bob", self.cache), {})      # ← 换账号即作废

    def test_legacy_flat_cache_is_invalidated(self):
        self.cache.write_text(json.dumps({"cid:clazz": {"url": "/work/getAllWork?enc=OLD"}}), encoding="utf-8")
        self.assertEqual(store.load_menu_cache("alice", self.cache), {})

    def test_cache_key_and_identity_helpers(self):
        self.assertEqual(store.menu_key("1", "2"), "1:2")


class EnsureSessionAccountSwitchTest(unittest.TestCase):
    """换账号时不能沿用旧 cookie —— 必须重新登录。"""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = mock.start()
        cls._saved = (L.PASSPORT, L.SSO, L.MOOC_API, L.PROTECTED_URL)
        L.PASSPORT = L.SSO = L.MOOC_API = cls.base
        L.PROTECTED_URL = cls.base + "/base"
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
        mock.STATE.login_ok = True
        mock.STATE.token = "goodtoken"
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "cookies.json"

    def _cfg(self, **kw) -> AuthConfig:
        base = dict(username=USER, password=PWD, state_path=self.state)
        base.update(kw)
        return AuthConfig(**base)

    def _write_state(self, account: str):
        save_cookies([Cookie("MOCKSESS", "goodtoken", domain="127.0.0.1", path="/",
                             expires=time.time() + 3600)], self.state, account=account)

    def test_foreign_cookie_is_ignored_and_we_relogin(self):
        self._write_state("someone_else")          # 旧账号的 cookie（在 mock 上依然"有效"）
        info = L.ensure_session(self._cfg())
        self.assertTrue(info.ok, info.message)
        self.assertEqual(info.source, "password")   # ← 没沿用旧会话
        self.assertEqual(len(mock.STATE.login_posts), 1)
        self.assertEqual(load_state(self.state).account, USER)   # 落盘改绑到当前账号

    def test_legacy_cookie_without_account_is_ignored(self):
        self.state.write_text(json.dumps({"saved_at": 1, "cookies": [
            {"name": "MOCKSESS", "value": "goodtoken", "domain": "127.0.0.1", "path": "/"}]}), encoding="utf-8")
        info = L.ensure_session(self._cfg())
        self.assertEqual(info.source, "password")
        self.assertEqual(len(mock.STATE.login_posts), 1)

    def test_same_account_cookie_is_reused(self):
        self._write_state(USER)
        info = L.ensure_session(self._cfg())
        self.assertEqual(info.source, "state-cookie")
        self.assertEqual(len(mock.STATE.login_posts), 0)   # ← 同账号才复用

    def test_cookie_blob_identity_is_respected(self):
        """只用 XUEXITONG_COOKIE 模式：blob 的 _uid 与磁盘状态不一致时也要重建。"""
        self._write_state("111222333")
        blob = "MOCKSESS=goodtoken; _uid=999888333"
        info = L.ensure_session(self._cfg(username="", password="", cookie_blob=blob))
        self.assertTrue(info.ok, info.message)
        self.assertEqual(info.source, "env-cookie")
        self.assertEqual(load_state(self.state).account, "999888333")


class DownloadRootIsolationTest(unittest.TestCase):
    """落盘路径必须按账号隔离，避免两个账号的同名作业互相覆盖。"""

    def test_resolved_root_appends_account(self):
        from xuexitong.attachments import resolved_root
        self.assertEqual(str(resolved_root("D:/学习通作业", "100000002")),
                         str(Path("D:/学习通作业") / "100000002"))

    def test_resolved_root_is_idempotent(self):
        """调用方已经传了账号目录时不能再叠一层。"""
        from xuexitong.attachments import resolved_root
        p = resolved_root("D:/学习通作业/100000002", "100000002")
        self.assertEqual(p.name, "100000002")
        self.assertEqual(str(resolved_root(p, "100000002")), str(p))

    def test_resolved_root_without_account_keeps_root(self):
        from xuexitong.attachments import resolved_root
        self.assertEqual(str(resolved_root("D:/学习通作业", "")), str(Path("D:/学习通作业")))

    def test_account_dir_name_prefers_uid_over_username(self):
        """优先用 uid：手机号不该出现在文件路径里。"""
        import httpx
        from xuexitong import store
        client = httpx.Client()
        client.cookies.set("_uid", "100000002", domain=".chaoxing.com")
        try:
            self.assertEqual(store.account_dir_name(client), "100000002")
        finally:
            client.close()

    def test_account_dir_name_falls_back_to_env_username(self):
        import os
        import httpx
        from unittest import mock as umock
        from xuexitong import store
        client = httpx.Client()          # 没有 cookie → 只能退到配置
        try:
            with umock.patch.dict(os.environ, {"XUEXITONG_USERNAME": "136/a"}, clear=False):
                self.assertEqual(store.account_dir_name(client), "136_a")   # 非法字符被替换
        finally:
            client.close()

    def test_account_dir_name_empty_when_nothing_known(self):
        import os
        import httpx
        from unittest import mock as umock
        from xuexitong import store
        client = httpx.Client()
        try:
            with umock.patch.dict(os.environ, {"XUEXITONG_USERNAME": ""}, clear=False):
                self.assertEqual(store.account_dir_name(client), "")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
