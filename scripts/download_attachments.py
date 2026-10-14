"""下载作业附件。

MCP 的正式功能：下载**未完成（待做）**作业的附件。
    python scripts/download_attachments.py                # 默认：待做作业的附件
测试模式（本账号没有待做作业，用来验证下载链路）：
    python scripts/download_attachments.py --mode completed-random --n 3

落盘：<root>/<作业名>/<附件名>，默认 root = D:/学习通作业

其他参数：
    --root D:/学习通作业   根目录
    --sleep 0.8            课程间间隔（避免反爬）
    --dump-dir state/detail 详情页里找不到附件时保存 HTML 供排查
    --dry-run              只列计划不下载
    --no-cache             不用课程菜单缓存
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong import AuthConfig, ensure_session                        # noqa: E402
from xuexitong.attachments import DEFAULT_ROOT, download_homework_attachments  # noqa: E402
from xuexitong.courses import (                                         # noqa: E402
    AntispiderError, FetchError, MOOC, SessionExpired,
    find_homework_menu, get_course_list, get_course_menu,
)
from xuexitong.homework import fetch_homework_list, status_distribution  # noqa: E402

from xuexitong.paths import state_dir  # noqa: E402

CACHE = state_dir() / "menu_cache.json"


def load_cache(use_cache: bool) -> dict:
    if not use_cache:
        return {}
    from xuexitong import store
    return store.load_menu_cache(store.current_identity())


def save_cache(cache: dict) -> None:
    from xuexitong import store
    if cache:
        store.save_menu_cache(cache, store.current_identity())


def homework_menu_url(client, course, cache: dict, use_cache: bool) -> str:
    key = f"{course.course_id}:{course.class_id}"
    if use_cache and cache.get(key, {}).get("url"):
        return cache[key]["url"]
    items, _ = get_course_menu(client, course)
    menu = find_homework_menu(items)
    if menu is None:
        raise FetchError(f"课程《{course.course_name}》菜单里没有'作业'入口")
    url = menu.effective_url or menu.data or menu.href
    cache[key] = {"url": url, "course_name": course.course_name}
    return url


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["unfinished", "completed-random", "completed-all"],
                    default="unfinished")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--sleep", type=float, default=0.8)
    ap.add_argument("--max-courses", type=int, default=0)
    ap.add_argument("--dump-dir", default="state/detail")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args(argv)

    cfg = AuthConfig.from_env()
    info = ensure_session(cfg)
    if not info.ok:
        print("会话不可用:", info.summary())
        return 2
    client = info.client
    print(f"会话: {info.summary()}")
    print(f"模式: {args.mode}   根目录: {args.root}")
    print()

    use_cache = not args.no_cache
    cache = load_cache(use_cache)
    courses = get_course_list(client)
    todo = courses if args.max_courses == 0 else courses[: args.max_courses]

    all_hw = []
    errors: list[str] = []
    for idx, course in enumerate(todo, 1):
        try:
            menu_url = homework_menu_url(client, course, cache, use_cache)
            menu_stub = type("M", (), {"effective_url": menu_url, "data": "", "href": "", "name": "作业"})
            hws, _ = fetch_homework_list(client, course, menu_stub)
            all_hw.extend(hws)
            dist = status_distribution(hws)
            if hws:
                print(f"[{idx:>2}/{len(todo)}] {course.course_name[:24]:<24} {len(hws):>2} 条 {dist}")
        except SessionExpired as e:
            errors.append(f"{course.course_name}: 会话失效 {e}")
            print(f"[{idx:>2}/{len(todo)}] {course.course_name[:24]:<24} [X] 会话失效，停止")
            break
        except AntispiderError as e:
            errors.append(f"{course.course_name}: 反爬未通过")
            print(f"[{idx:>2}/{len(todo)}] {course.course_name[:24]:<24} [X] 反爬，停止扫描")
            break
        except FetchError as e:
            errors.append(f"{course.course_name}: {e}")
        if args.sleep:
            time.sleep(args.sleep)

    if use_cache:
        save_cache(cache)

    print()
    print(f"共扫描 {len(todo)} 门课，抓到作业 {len(all_hw)} 条，状态分布 {status_distribution(all_hw)}")

    if args.mode == "unfinished":
        targets = [h for h in all_hw if h.is_unfinished_strict]
        print(f"目标（待做，MCP 正式行为）: {len(targets)} 条")
    else:
        done = [h for h in all_hw if h.status_num == 2]
        print(f"已完成作业: {len(done)} 条")
        if args.mode == "completed-random":
            random.seed()                      # 真随机
            targets = random.sample(done, min(args.n, len(done)))
            print(f"目标（随机 {len(targets)} 条已完成）: {[h.name[:24] for h in targets]}")
        else:
            targets = done

    if not targets:
        print("没有目标作业可下载。")
        if errors:
            print(f"扫描期异常 {len(errors)} 项:")
            for e in errors:
                print(f"    ! {e}")
        return 0

    print()
    if args.dry_run:
        for h in targets:
            print(f"  [dry-run] 《{h.name}》 status={h.status_raw} → {args.root}/{h.name}/")
        return 0

    total_files = total_bytes = 0
    for i, hw in enumerate(targets, 1):
        print(f"--- [{i}/{len(targets)}] 《{hw.name}》 status={hw.status_raw}")
        print(f"    详情: {(hw.detail_url or hw.url)[:110]}")
        res = download_homework_attachments(client, hw, root=args.root,
                                            overwrite=args.overwrite, dump_dir=args.dump_dir)
        if res.get("error"):
            print(f"    [X] {res['error']}")
            errors.append(f"{hw.name}: {res['error']}")
            continue
        print(f"    附件 {res['total']} 个 → 下载 {res['downloaded']}，跳过 {res['skipped']}，失败 {res['failed']}")
        if res.get("note"):
            print(f"    [!] {res['note']}"
                  + (f"（已保存 HTML: {res['html_dump']}）" if res.get("html_dump") else ""))
        for a in res["attachments"]:
            flag = "OK" if a["downloaded"] else ("SKIP" if a["skipped"] else "FAIL")
            print(f"      [{flag}] {a['filename'] or a['object_id']:<40} {a['size']:>9} B  {a['error']}")
            if a["downloaded"]:
                total_files += 1
                total_bytes += a["size"]
        print(f"    目录: {res['folder']}")
        if args.sleep:
            time.sleep(args.sleep)

    print()
    print("=" * 70)
    print(f"下载完成：{total_files} 个文件，{total_bytes/1024:.1f} KB")
    print(f"根目录: {args.root}")
    if errors:
        print(f"异常 {len(errors)} 项:")
        for e in errors:
            print(f"    ! {e}")
    print("=" * 70)
    if info.client:
        info.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
