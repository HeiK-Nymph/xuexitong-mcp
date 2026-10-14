"""实测：哪条登录路径是可行的（"最好的路径效果"）。

只读部分（默认就跑）：
  A. 登录页 needVcode —— 判断当前是否已被要求滑动验证
  B. SSO 探测接口在【未登录】时的真实响应形状 —— 决定 probe() 的判据是否可靠
写操作部分（需 --yes）：
  C. 用一条明显不存在的测试账号，对比 3 种 fanyalogin 请求体的服务端反馈：
     C1 AES 加密 + t=true（官方正确形态）
     C2 明文 + t=true（参考实现 chaoxing_mcp 的形态）
     C3 明文 + t=false
     判据：若某条返回"账号/密码错误"类业务错误 → 该形态被服务端正确解析；
           若返回格式/解密类错误或异常 → 该形态本身是错的。

用法：
    python scripts/probe_login_paths.py            # 只跑 A/B
    python scripts/probe_login_paths.py --yes      # 追加 C（会发 3 次登录请求，账号是假的）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong.crypto import encrypt                      # noqa: E402
from xuexitong.login import (                             # noqa: E402
    PASSPORT, SSO, build_login_data, fetch_login_page, probe,
)
from xuexitong.session import build_client                # noqa: E402

BOGUS_USER = "13900000000"          # 保留的测试号段，明显不属于任何真实用户
BOGUS_PWD = "NotARealPassword-000"


def step_a(client) -> bool:
    t0 = time.perf_counter()
    html = fetch_login_page(client)
    need = 'id="needVcode"[^>]*value="1"' in html
    ms = int((time.perf_counter() - t0) * 1000)
    print(f"[A] 登录页 needVcode=1 ? {'是（需要滑动验证）' if need else '否（当前不要求验证码）'}   "
          f"HTML {len(html)} 字节，{ms}ms")
    return need


def step_b(client) -> None:
    t0 = time.perf_counter()
    valid, uid, name, raw = probe(client)
    ms = int((time.perf_counter() - t0) * 1000)
    print(f"[B] 未登录时探测结果: valid={valid} uid='{uid}' name='{name}'  ({ms}ms)")
    print(f"    SSO 原始响应: {json.dumps(raw, ensure_ascii=False)[:300]}")

    from xuexitong.login import probe_business
    b_ok, b_n = probe_business(client)
    print(f"    业务接口 backclazzdata: ok={b_ok} 课程数={b_n}")


def step_c(client, need_vcode: bool) -> None:
    if need_vcode:
        print("[C] 跳过：当前已要求滑动验证，再发登录请求会加剧风控。")
        return
    cases = [
        ("C1 AES + t=true（官方形态）", True, "true"),
        ("C2 明文 + t=true（参考实现）", False, "true"),
        ("C3 明文 + t=false", False, "false"),
    ]
    print(f"[C] 用测试账号 {BOGUS_USER} 对比 3 种请求体（若返回业务错误即说明该形态被正确解析）：")
    for label, enc, t in cases:
        data = build_login_data(BOGUS_USER, BOGUS_PWD, encrypt_payload=enc, t=t)
        t0 = time.perf_counter()
        try:
            resp = client.post(f"{PASSPORT}/fanyalogin", data=data, timeout=10)
            ms = int((time.perf_counter() - t0) * 1000)
            body = resp.text[:200].replace("\n", " ")
            try:
                payload = resp.json()
                status = payload.get("status")
                msg = payload.get("msg2") or payload.get("msg") or ""
                print(f"    {label:<28} HTTP {resp.status_code} status={status} msg2='{msg}' ({ms}ms)")
            except ValueError:
                print(f"    {label:<28} HTTP {resp.status_code} 非 JSON: {body}")
        except Exception as e:  # noqa: BLE001
            print(f"    {label:<28} 请求异常: {e}")
        time.sleep(2)


def main(argv: list[str]) -> int:
    do_login_probe = "--yes" in argv
    with build_client() as client:
        need = step_a(client)
        step_b(client)
        print()
        if do_login_probe:
            step_c(client, need)
        else:
            print("[C] 未加 --yes，跳过写操作探测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
