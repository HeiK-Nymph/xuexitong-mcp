"""真实账号抓取：课程列表 → 每门课作业列表 → 状态文案分布。

用法：
    python scripts/fetch_all.py                      # 默认前 5 门（温和，避免触发风控）
    python scripts/fetch_all.py --limit 0            # 全部课程
    python scripts/fetch_all.py --dump-dir state/dump # 顺便保存作业列表页 HTML
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong import AuthConfig, ensure_session                      # noqa: E402
from xuexitong.courses import (                                       # noqa: E402
    AntispiderError, FetchError, SessionExpired,
    find_homework_menu, get_course_list, get_course_menu,
)
from xuexitong.homework import fetch_homework_list, status_distribution  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--sleep", type=float, default=0.6)
    ap.add_argument("--dump-dir", default="")
    args = ap.parse_args(argv)

    cfg = AuthConfig.from_env()
    info = ensure_session(cfg)
    if not info.ok:
        print("会话不可用:", info.summary())
        return 2
    client = info.client
    print(f"会话: {info.summary()}")
    print()

    try:
        courses = get_course_list(client)
    except FetchError as e:
        print(f"[X] 获取课程列表失败: {e}")
        return 3
    print(f"课程列表: 共 {len(courses)} 门")
    for c in courses[:10]:
        print(f"    - {c.course_name}  (course_id={c.course_id} class_id={c.class_id} cpi={c.cpi})")
    if len(courses) > 10:
        print(f"    ... 其余 {len(courses) - 10} 门略")
    print()

    todo = courses if args.limit == 0 else courses[: args.limit]
    all_status: collections.Counter = collections.Counter()
    strict_total = loose_total = hw_total = 0
    errors: list[str] = []

    for idx, course in enumerate(todo, 1):
        head = f"[{idx}/{len(todo)}] {course.course_name}"
        try:
            items, strategy = get_course_menu(client, course)
            menu = find_homework_menu(items)
            if menu is None:
                names = [i.name for i in items][:12]
                errors.append(f"{course.course_name}: 菜单无'作业'项 (策略 {strategy}, 菜单项 {names})")
                print(f"{head}")
                print(f"    [!] 菜单里没找到'作业'项（策略 {strategy}）菜单项: {names}")
                continue

            dump = str(Path(args.dump_dir) / f"{course.course_id}.html") if args.dump_dir else None
            hws, html = fetch_homework_list(client, course, menu, dump_html_to=dump)

            dist = status_distribution(hws)
            strict = [h for h in hws if h.is_unfinished_strict]
            loose = [h for h in hws if h.is_not_done]
            hw_total += len(hws)
            strict_total += len(strict)
            loose_total += len(loose)
            all_status.update(dist)

            print(f"{head}")
            print(f"    作业入口: {menu.name!r} -> {menu.effective_url[:200]}")
            print(f"    作业 {len(hws)} 条; 状态文案分布 {dist}")
            print(f"    严格未完成(待做) {len(strict)}; 宽松未做完 {len(loose)}")
            for h in hws[:3]:
                print(f"      . {h.name[:36]:<36} status={h.status_raw!r} spans={list(h.raw_spans)[:3]}")
        except SessionExpired as e:
            print(f"{head}\n    [X] 会话失效: {e}")
            errors.append(f"{course.course_name}: 会话失效")
            break
        except AntispiderError as e:
            print(f"{head}\n    [X] 反爬未通过: {e}")
            errors.append(f"{course.course_name}: 反爬未通过")
            break
        except FetchError as e:
            print(f"{head}\n    [X] {e}")
            errors.append(f"{course.course_name}: {e}")
        if args.sleep:
            time.sleep(args.sleep)

    print()
    print("=" * 70)
    print(f"扫描课程 {len(todo)} 门; 作业总数 {hw_total}")
    print(f"严格口径(待做, chaoxing_mcp 规则) 未完成: {strict_total}")
    print(f"宽松口径(非完成态文案) 未做完: {loose_total}")
    print(f"全部状态文案分布: {dict(all_status)}")
    if errors:
        print(f"失败/异常 {len(errors)} 项:")
        for e in errors:
            print(f"    ! {e}")
    print("=" * 70)
    if info.client:
        info.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
