"""真实账号 A/B：AES 形态 vs 明文形态（需要 --yes，且只在你自己的账号上跑）。

背景：用假账号无法区分两者——账号不存在时服务端在"查用户"阶段就短路了，
      三种形态都返回同一句"用户名或密码错误"。只有真实账号才会走到密码比对。

策略（最少请求、任一成功即停）：
    1) 先读 needVcode；若=1 直接放弃（会被风控拦，不硬试）
    2) 试 AES + t=true（官方形态）
    3) 仅当 2) 返回业务性失败（用户名或密码错误）时，才试明文 + t=true（参考实现形态）
    4) 输出对照结论

用法：
    set XUEXITONG_USERNAME=... & set XUEXITONG_PASSWORD=...
    python scripts/ab_login.py --yes
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong import AuthConfig  # noqa: E402
from xuexitong.login import (  # noqa: E402
    LoginOutcome, login_with_password, read_need_vcode,
)
from xuexitong.session import build_client, cookies_from_client, save_cookies  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="确认用自己的账号发起真实登录")
    args = ap.parse_args(argv)
    cfg = AuthConfig.from_env()
    if not cfg.has_password:
        print("缺少 XUEXITONG_USERNAME / XUEXITONG_PASSWORD"); return 2
    if not args.yes:
        print("这会用你的真实账号发登录请求（最多 2 次）。确认请加 --yes"); return 2

    with build_client(insecure=cfg.insecure) as client:
        if read_need_vcode(client):
            print("登录页当前要求滑动验证（needVcode=1）——请先用浏览器登录一次，")
            print("或直接提供 XUEXITONG_COOKIE。已放弃，不做硬试。")
            return 3

        rows = []
        enc = login_with_password(client, cfg.username, cfg.password, encrypt_payload=True, t="true")
        rows.append(("AES + t=true（官方形态）", enc))
        print(f"[1] AES   -> {enc.outcome.value}  msg={enc.message!r}  ({enc.status_code})")

        if enc.outcome is LoginOutcome.OK:
            print("AES 形态成功；无需再试明文。")
        elif enc.outcome is LoginOutcome.BAD_CREDENTIALS:
            time.sleep(2)
            plain = login_with_password(client, cfg.username, cfg.password, encrypt_payload=False, t="true")
            rows.append(("明文 + t=true（参考实现）", plain))
            print(f"[2] 明文  -> {plain.outcome.value}  msg={plain.message!r}  ({plain.status_code})")
        else:
            print(f"AES 形态非业务失败（{enc.outcome.value}），不做明文对照，避免加剧风控。")

        print()
        print("结论对照：")
        for label, res in rows:
            print(f"  {label:<26} outcome={res.outcome.value:<22} msg2={res.message!r}")

        if enc.outcome is LoginOutcome.OK:
            cookies = cookies_from_client(client)
            path = save_cookies(cookies, cfg.state_path)
            print(f"\n已保存 {len(cookies)} 张 cookie 到 {path}")
            return 0
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
