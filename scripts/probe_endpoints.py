"""只读诊断：候选探测端点在【未登录】状态下长什么样。

目标是为 probe() 选出可靠的"会话是否有效"判据 —— 参考实现用"cookie 名是否存在"
判断，会产生假成功；我们要的是服务端明确表态。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong.session import build_client  # noqa: E402

CANDIDATES = [
    ("SSO userLogin4UAP", "https://sso.chaoxing.com/apis/login/userLogin4UAP.do"),
    ("个人主页 i.chaoxing.com/base", "https://i.chaoxing.com/base"),
    ("课程列表 backclazzdata", "https://mooc1-api.chaoxing.com/mycourse/backclazzdata"),
    ("mooc1-2 首页", "https://mooc1-2.chaoxing.com/visit/interaction"),
]


def main() -> int:
    with build_client() as c:
        for label, url in CANDIDATES:
            try:
                resp = c.get(url, follow_redirects=False, timeout=10)
                body = (resp.text or "").strip().replace("\n", " ")[:160]
                loc = resp.headers.get("location", "")
                print(f"### {label}")
                print(f"    {url}")
                print(f"    HTTP {resp.status_code}  location={loc[:110]}")
                print(f"    body: {body}")
            except Exception as e:  # noqa: BLE001
                print(f"### {label}\n    异常: {type(e).__name__}: {e}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
