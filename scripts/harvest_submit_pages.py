"""收割"新版作业作答页"的 URL（含新版 enc），写入 state/submit/pages.json。

为什么需要它：新版作答页的 enc 由页面自己下发，老版作业列表页拿到的 enc 直连会 403；
新课程页是 SPA（静态 GET 只有空壳）。所以首次必须过浏览器拿一次真实 URL。

两种模式（都不需要借用你的标签页，因此不会弹借用确认）：
  1) --from-tabs   扫描你已打开的标签页，凡是 /mooc-ans/mooc2/work/dowork 的都收下来（最省事：
                   你手动把 N 个作业点开即可）
  2) --url <entry> 用"任务标签页"打开给定入口链接，读重定向后的最终 URL（用于待做作业首次进入）
收尾一律先 window.isContentChanged=false，避免 beforeunload 原生弹窗把关闭卡住。

用法：
  python scripts/harvest_submit_pages.py --from-tabs
  python scripts/harvest_submit_pages.py --url "https://mooc1.chaoxing.com/…"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong.paths import state_file  # noqa: E402

PAGES = state_file("submit", "pages.json")
BSK = os.environ.get("BSK_EXE") or str(Path.home() / ".local" / "bin" / "bsk.exe")
DOWORK_RE = re.compile(r"https://mooc1\.chaoxing\.com/mooc-ans/mooc2/work/dowork\?[^\s\"'<>]+")


def bsk(*args: str, timeout: int = 120) -> str:
    env = dict(os.environ, BSK_AUTO_START="0")
    r = subprocess.run([BSK, *args], capture_output=True, text=True, errors="replace",
                       timeout=timeout, env=env)
    return (r.stdout or "") + (r.stderr or "")


def load_pages() -> dict:
    if PAGES.is_file():
        try:
            return json.loads(PAGES.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_pages(pages: dict) -> None:
    PAGES.parent.mkdir(parents=True, exist_ok=True)
    PAGES.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")


def record(url: str, pages: dict) -> str:
    q = parse_qs(urlparse(url).query)
    wid = (q.get("workId") or [""])[0]
    if not wid:
        return ""
    pages[wid] = {"page_url": url, "answer_id": (q.get("answerId") or [""])[0],
                  "standard_enc": (q.get("standardEnc") or [""])[0],
                  "enc": (q.get("enc") or [""])[0], "harvested_at": int(time.time())}
    return wid


def from_tabs() -> int:
    """扫描用户已打开的标签页（只读列表，不需要借用）。"""
    out = bsk("tab", "list", "--scope", "user", "--json")
    try:
        tabs = json.loads(out).get("tabs", [])
    except ValueError:
        # 没有会话时先建一个再列 —— 用完必须关掉，否则会漏会话（实测踩过）
        m = re.search(r'"session_id"\s*:\s*"([^"]+)"', bsk("session", "start", "--json"))
        sid = m.group(1) if m else ""
        try:
            out = bsk("tab", "list", "--scope", "user", "--session", sid, "--json")
            tabs = json.loads(out).get("tabs", [])
        finally:
            if sid:
                bsk("session", "stop", sid, "--json", timeout=60)
    pages = load_pages()
    found = 0
    for t in tabs:
        url = t.get("url") or ""
        if "/mooc-ans/mooc2/work/dowork" in url:
            wid = record(url, pages)
            if wid:
                found += 1
                print(f"  收割 work_id={wid}  ← {t.get('title')}")
    save_pages(pages)
    print(f"[from-tabs] 新收割 {found} 个；pages.json 现有 {len(pages)} 条 → {PAGES}")
    return found


def from_urls(urls: list[str]) -> int:
    """用任务标签页打开入口链接，读最终 URL。"""
    sid = ""
    txt = bsk("session", "start", "--json")
    m = re.search(r'"session_id"\s*:\s*"([^"]+)"', txt)
    if m:
        sid = m.group(1)
    if not sid:
        print("[!] 起不来 bsk 会话（daemon 没跑？）。先在一个长驻终端执行：")
        print("    $env:BSK_AUTO_START='0'; bsk daemon start --foreground")
        return 0
    tid = ""
    m = re.search(r'"tab_id"\s*:\s*(\d+)', bsk("tab", "create", "--no-active", "--session", sid, "--json"))
    if m:
        tid = m.group(1)
    pages = load_pages()
    found = 0
    try:
        for u in urls:
            bsk("navigate", u, "--session", sid, *(["--tab-id", tid] if tid else []))
            time.sleep(2.5)
            cur = bsk("evaluate", "--session", sid, *(["--tab-id", tid] if tid else []),
                      "--json", "location.href")
            final = ""
            mm = re.search(r'"value"\s*:\s*"([^"]+)"', cur)
            if mm:
                final = mm.group(1).replace("\\/", "/")
            if "/mooc-ans/mooc2/work/dowork" not in final:
                print(f"  [!] {u[:60]}… → 最终 URL 不是作答页：{final[:90]}")
                continue
            wid = record(final, pages)
            if wid:
                found += 1
                print(f"  收割 work_id={wid}  ← {final[:90]}")
    finally:
        # 关键收尾：清脏标记，否则关闭/导航会撞 beforeunload 原生弹窗（脚本控制不到）
        bsk("evaluate", "--session", sid, *(["--tab-id", tid] if tid else []),
            "--json", "window.isContentChanged=false; 'ok'")
        bsk("session", "stop", sid, "--json", timeout=60)
    save_pages(pages)
    print(f"[from-urls] 新收割 {found} 个；pages.json 现有 {len(pages)} 条 → {PAGES}")
    return found


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-tabs", action="store_true")
    ap.add_argument("--url", action="append", default=[])
    args = ap.parse_args(argv)
    if not args.from_tabs and not args.url:
        print(__doc__)
        return 2
    if args.url:
        from_urls(args.url)
    if args.from_tabs:
        from_tabs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
