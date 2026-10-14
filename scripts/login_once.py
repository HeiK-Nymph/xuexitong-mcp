"""真实登录入口 —— 读 env，走最优路径，落盘 cookie。

用法：
    python scripts/login_once.py                 # cookie 优先，失效才用账号密码
    python scripts/login_once.py --force-login   # 强制重新登录
    python scripts/login_once.py --business      # 额外验证业务接口（课程列表）
    python scripts/login_once.py --smoke         # 不发登录请求，只验证探测端点可达

凭据来自环境变量（兜底：项目根 .env → <作业根>/.xuexitong_mcp/.env）：
    XUEXITONG_USERNAME / XUEXITONG_PASSWORD / XUEXITONG_COOKIE / XUEXITONG_STATE_PATH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong import AuthConfig, ensure_session  # noqa: E402
from xuexitong.login import probe, probe_business  # noqa: E402
from xuexitong.session import build_client  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-login", action="store_true")
    ap.add_argument("--business", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args(argv)

    cfg = AuthConfig.from_env()
    print(f"配置      : {cfg.describe()}")
    print(f"cookie 文件: {cfg.state_path}")

    if args.smoke:
        with build_client(insecure=cfg.insecure) as client:
            valid, uid, name, detail = probe(client)
            b_ok, b_n = probe_business(client)
            print(f"探测(未带凭据): valid={valid} detail={detail}")
            print(f"业务接口      : ok={b_ok} 课程数={b_n}")
            print("预期：valid=False（无凭据时应被踢回登录页），能跑到这里说明端点和 TLS 都通。")
        return 0

    info = ensure_session(cfg, force_login=args.force_login, verify_business=args.business)
    print()
    print("结果:", info.summary())
    if info.client is not None:
        if info.ok and args.business:
            ok, n = probe_business(info.client)
            print(f"业务接口: ok={ok} 课程数={n}")
        info.client.close()
    return 0 if info.ok else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
