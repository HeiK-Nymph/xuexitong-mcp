"""实测 getAllWork 的 status / pageNum 参数：同一门课对比不同取值的结果。

用法：
    python scripts/probe_status_param.py                    # 默认软件工程那门课
    python scripts/probe_status_param.py --course 266665001 --class 153554001
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong import AuthConfig, ensure_session                      # noqa: E402
from xuexitong.courses import (                                       # noqa: E402
    AntispiderError, FetchError, MOOC, find_homework_menu, get_course_list, get_course_menu,
)
from xuexitong.homework import parse_homework_list, status_distribution  # noqa: E402

STATUS_CANDIDATES = [None, "-1", "0", "1", "2"]


def with_params(url: str, **extra) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    q.update({k: v for k, v in extra.items() if v is not None})
    return urlunparse(p._replace(query=urlencode(q)))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", default="111000111")
    ap.add_argument("--class", dest="clazz", default="111222333")
    ap.add_argument("--sleep", type=float, default=1.5)
    args = ap.parse_args(argv)

    cfg = AuthConfig.from_env()
    info = ensure_session(cfg)
    if not info.ok:
        print("会话不可用:", info.summary())
        return 2
    client = info.client

    courses = get_course_list(client)
    course = next((c for c in courses
                   if c.course_id == args.course and c.class_id == args.clazz), None)
    if course is None:
        print(f"课程 {args.course}/{args.clazz} 不在课程列表里")
        return 3
    print(f"课程: {course.course_name}  course_id={course.course_id} class_id={course.class_id}")
    try:
        items, _ = get_course_menu(client, course)
    except (FetchError, AntispiderError) as e:
        print(f"[X] 菜单抓取失败: {e}")
        return 4
    menu = find_homework_menu(items)
    if menu is None:
        print("[X] 没找到作业入口")
        return 5
    base = MOOC + (menu.effective_url if menu.effective_url.startswith("/") else "/work/getAllWork")

    print(f"基础 URL: {base[:150]}")
    print()
    for st in STATUS_CANDIDATES:
        url = with_params(base, status=st)
        try:
            resp = client.get(url, timeout=20)
            if resp.status_code == 202 or "antispiderShowVerify" in str(resp.url):
                print(f"status={st!r:<6} -> 命中反爬，停止后续试验")
                break
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"status={st!r:<6} -> 请求异常 {e}")
            continue
        hws = parse_homework_list(resp.text, course, url)
        dist = status_distribution(hws)
        names = [h.name[:20] for h in hws[:5]]
        print(f"status={st!r:<6} 作业 {len(hws):>3} 条  文案分布 {dist}")
        print(f"            前几条: {names}")
        time.sleep(args.sleep)

    print()
    print("--- pageNum 试验（不带 status）---")
    for pn in ("1", "2"):
        url = with_params(base, pageNum=pn)
        try:
            resp = client.get(url, timeout=20)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"pageNum={pn} -> 异常 {e}")
            continue
        hws = parse_homework_list(resp.text, course, url)
        print(f"pageNum={pn} 作业 {len(hws)} 条  分布 {status_distribution(hws)}")
        time.sleep(args.sleep)

    if info.client:
        info.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
