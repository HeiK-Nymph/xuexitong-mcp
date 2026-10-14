"""cookie 持久化测试 —— 重点验证"过期即丢弃"和"原子写入"。"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xuexitong.session import (  # noqa: E402
    Cookie, apply_cookies, build_client, load_cookies, parse_cookie_blob, save_cookies,
)


class TestSession(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "cookies.json"

    def test_save_load_roundtrip_preserves_fields(self):
        cookies = [
            Cookie("_uid", "123", domain=".chaoxing.com", path="/", expires=time.time() + 3600),
            Cookie("UID", "123", domain=".chaoxing.com", path="/"),
            Cookie("vc3", "abc", domain="mooc1-2.chaoxing.com", path="/", http_only=True, secure=True),
        ]
        save_cookies(cookies, self.state)
        loaded = {c.name: c for c in load_cookies(self.state)}
        self.assertEqual(set(loaded), {"_uid", "UID", "vc3"})
        self.assertEqual(loaded["vc3"].domain, "mooc1-2.chaoxing.com")
        self.assertTrue(loaded["vc3"].http_only)
        self.assertTrue(loaded["vc3"].secure)
        self.assertIsNone(loaded["UID"].expires)

    def test_expired_cookies_are_dropped_on_load(self):
        """这是参考实现"假成功"的根治点：过期 cookie 不能再被当成已登录。"""
        save_cookies([
            Cookie("_uid", "123", expires=time.time() - 1),
            Cookie("UID", "123", expires=time.time() + 60),
        ], self.state)
        names = {c.name for c in load_cookies(self.state)}
        self.assertEqual(names, {"UID"})

    def test_atomic_write_leaves_no_tmp_file(self):
        save_cookies([Cookie("a", "1")], self.state)
        self.assertTrue(self.state.is_file())
        self.assertFalse(self.state.with_suffix(".json.tmp").exists())

    def test_corrupt_file_is_treated_as_empty(self):
        self.state.write_text("{ not json", encoding="utf-8")
        self.assertEqual(load_cookies(self.state), [])

    def test_apply_cookies_keeps_domain_and_path(self):
        client = build_client()
        try:
            n = apply_cookies(client, [
                Cookie("_uid", "123", domain=".chaoxing.com", path="/"),
                Cookie("expired", "x", expires=time.time() - 5),
            ])
            self.assertEqual(n, 1)                      # 过期的不注入
            got = {(c.name, c.domain, c.path) for c in client.cookies.jar}
            self.assertIn(("_uid", ".chaoxing.com", "/"), got)
        finally:
            client.close()

    def test_parse_cookie_blob_defaults_to_chaoxing_domain(self):
        """真实场景：浏览器扩展复制的 name=value 要落到 .chaoxing.com，否则跨域带不出去。"""
        cookies = parse_cookie_blob("_uid=123", default_domain=None)
        self.assertEqual(cookies[0].domain, ".chaoxing.com")

    def test_parse_cookie_blob_can_override_domain(self):
        self.assertEqual(parse_cookie_blob("_uid=123", default_domain="127.0.0.1")[0].domain, "127.0.0.1")

    def test_parse_cookie_blob(self):
        cookies = parse_cookie_blob("_uid=123; UID=123\nvc3=abc;")
        self.assertEqual([c.name for c in cookies], ["_uid", "UID", "vc3"])
        self.assertEqual(cookies[2].value, "abc")

    def test_state_file_contains_no_password_like_field(self):
        save_cookies([Cookie("_uid", "123")], self.state)
        raw = self.state.read_text(encoding="utf-8")
        self.assertNotIn("password", raw)
        self.assertIn("saved_at", json.loads(raw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
