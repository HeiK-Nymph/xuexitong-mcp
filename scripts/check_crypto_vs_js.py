"""跨实现对照：Python 的 AES 实现 vs 官方 login.js 的真 CryptoJS 实现。

用法：python scripts/check_crypto_vs_js.py
前提：node + crypto-js（脚本会自动在 scripts/js_ref 下 npm install）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong.crypto import encrypt  # noqa: E402

JS_REF = ROOT / "scripts" / "js_ref"
NODE = "node"

SAMPLES = [
    "13800138000",            # 典型账号形态
    "Test-password-123",      # 普通密码
    "中文密码测试",            # 多字节
    "a",                      # 单字符
    "",                       # 空串（PKCS7 边界）
    "x" * 200,                # 多分组
]


def ensure_crypto_js() -> None:
    if (JS_REF / "node_modules" / "crypto-js").exists():
        return
    subprocess.run(["npm", "install", "crypto-js", "--no-audit", "--no-fund"],
                   cwd=JS_REF, check=True, shell=True)


def js_encrypt(samples: list[str]) -> list[str]:
    out = subprocess.run(
        [NODE, str(JS_REF / "encrypt_by_aes.js"), json.dumps(samples)],
        cwd=JS_REF, capture_output=True, text=True, check=True, shell=False,
    )
    return json.loads(out.stdout)


def main() -> int:
    ensure_crypto_js()
    expected = js_encrypt(SAMPLES)
    bad = 0
    print(f"{'样本':<22}{'JS(CryptoJS)':<26}{'Python':<26}一致")
    for s, js_val in zip(SAMPLES, expected):
        py_val = encrypt(s)
        same = py_val == js_val
        bad += 0 if same else 1
        label = s if len(s) <= 20 else s[:17] + "..."
        print(f"{label:<22}{js_val[:24]:<26}{py_val[:24]:<26}{'[OK]' if same else '[FAIL]'}")
    vectors = ROOT / "tests" / "js_vectors.json"
    vectors.parent.mkdir(parents=True, exist_ok=True)
    vectors.write_text(json.dumps(dict(zip(SAMPLES, expected)), ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"向量已写入 {vectors}")
    print()
    if bad:
        print(f"[FAIL] {bad}/{len(SAMPLES)} 个样本不一致 —— AES 实现不可用")
        return 1
    print(f"[OK] {len(SAMPLES)}/{len(SAMPLES)} 个样本与官方 JS 逐字节一致（AES-128-CBC/PKCS7/IV=key/Base64）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
