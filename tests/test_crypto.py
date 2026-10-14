"""AES 实现的单元测试。

期望值不手抄：由 scripts/check_crypto_vs_js.py 用**真 crypto-js** 跑官方 encryptByAES
产出 tests/js_vectors.json，这里再逐个比对。向量文件缺失时跳过该用例（不假装通过）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong.crypto import TRANSFER_KEY, decrypt, encrypt  # noqa: E402

VECTORS = ROOT / "tests" / "js_vectors.json"


class TestCrypto(unittest.TestCase):
    def test_key_is_16_bytes(self):
        self.assertEqual(len(TRANSFER_KEY), 16)
        self.assertEqual(TRANSFER_KEY, b"u2oh6Vu^HWe4_AES")

    def test_matches_official_js_vectors(self):
        if not VECTORS.is_file():
            self.skipTest("tests/js_vectors.json 不存在，先跑 scripts/check_crypto_vs_js.py")
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(vectors), 5)
        for plain, expected in vectors.items():
            with self.subTest(plain=plain[:12]):
                self.assertEqual(encrypt(plain), expected)

    def test_roundtrip(self):
        for plain in ["p@ss w0rd", "中文密码测试", "x" * 200, "", "13800138000"]:
            with self.subTest(plain=plain[:10]):
                self.assertEqual(decrypt(encrypt(plain)), plain)

    def test_output_is_base64_not_hex(self):
        out = encrypt("a")                      # 16B 单分组 → Base64 24 字符
        self.assertTrue(out.endswith("=="))
        self.assertEqual(len(out), 24)

    def test_deterministic(self):
        self.assertEqual(encrypt("same"), encrypt("same"))

    def test_rejects_bad_key(self):
        with self.assertRaises(ValueError):
            encrypt("x", b"shortkey")


if __name__ == "__main__":
    unittest.main(verbosity=2)
