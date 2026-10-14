# -*- coding: utf-8 -*-
"""纯 HTTP 发现「新版作答页」地址（含每作业独立的 enc）—— **不需要浏览器、也不需要用户给任何 URL**。

实测链路（2026-10-13，uid=100000002；先说 2026-10-13 早些时候的结论已经作废）：

  ⓪ 课程菜单页（MCP 本来就会抓）里带 `enc` / `openc` / `cpi`
     GET https://mooc1-2.chaoxing.com/visit/stucoursemiddle?courseid&clazzid&vc=1&cpi
     → 菜单项 URL 形如 /work/getAllWork?classId&courseId&isdisplaytable=2&mooc=1&ut=s&enc=<32hex>&cpi=…&openc=<32hex>

  ① "体验新版"课程页（**关键：必须带 v=2&fromMiddle=1**，少一个参数就只有 20KB 的旧页）
     GET https://mooc1.chaoxing.com/mooc-ans/mycourse/studentcourse
         ?courseId&clazzid&ut=s&enc=<⓪的 enc>&cpi&openc=<⓪的 openc>&v=2&fromMiddle=1
     → 99,078 字节，页面里带 <input id="workEnc" value="<作业列表 enc>">

  ② 新版作业列表页（t 用当前毫秒实测有效）
     GET https://mooc1.chaoxing.com/mooc2/work/list
         ?courseId&classId&cpi&ut=s&t=<now_ms>&stuenc=<⓪的 enc>&enc=<① 的 #workEnc>
     → 每个作业一行 data="…/mooc-ans/mooc2/work/task?…&workId&answerId&enc=<该作业自己的 enc>"

  ③ 作答页：打开 ② 的 task 链接，站点会给出
       · /mooc-ans/mooc2/work/dowork?…  可作答（有 uploadEnc/submitForm）✔
       · /mooc-ans/mooc2/work/view?…    **作业已截止**，只有查看页（明确上报，不硬凑）

曾经以为"必须用户给一次新版课程页 URL"（因为 `mooc2-ans…/mycourse/stu` 的 enc/t 造不出来）。
后来发现：那个 99,078 字节的页面用 `studentcourse?v=2&fromMiddle=1` **同样能拿到**，而它的参数
（enc/openc）就藏在课程菜单 URL 里 —— 于是整条链 100% 自动。
`register_study_page` 保留为兜底（万一某门课菜单里没有 v=2 入口）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import httpx

WORK_LIST_URL = "https://mooc1.chaoxing.com/mooc2/work/list"
STUDENTCOURSE_URL = "https://mooc1.chaoxing.com/mooc-ans/mycourse/studentcourse"
STU_HOST = "https://mooc2-ans.chaoxing.com"

_WORK_ENC_RE = re.compile(r'id="workEnc"[^>]*value="([0-9a-f]{32})"')
_TASK_DATA_RE = re.compile(r'data="([^"]*work/task\?[^"]*)"')
_HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b")


def parse_work_enc(html: str) -> str:
    """从新版学生课程页里取 #workEnc（作业列表 enc）。"""
    m = _WORK_ENC_RE.search(html or "")
    if m:
        return m.group(1)
    m = re.search(r"workEnc[^0-9a-f]{0,40}([0-9a-f]{32})", html or "")
    return m.group(1) if m else ""


def parse_work_encs(html: str) -> dict[str, dict]:
    """从新版作业列表页里解析每个作业的 answerId / enc / task_url。"""
    out: dict[str, dict] = {}
    for m in _TASK_DATA_RE.finditer(html or ""):
        raw = m.group(1).replace("&amp;", "&")
        q = dict(re.findall(r"([A-Za-z_]\w*)=([^&\"']*)", raw))
        wid = q.get("workId", "")
        if wid and wid not in out:
            out[wid] = {"work_id": wid, "answer_id": q.get("answerId", ""),
                        "enc": q.get("enc", ""), "task_url": raw}
    return out


def parse_stu_url(url: str) -> dict:
    """把用户给的新版课程页 URL 拆成参数（courseid/clazzid/cpi/enc/t）。"""
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url or "").query)
    return {
        "course_id": (q.get("courseid") or q.get("courseId") or [""])[0],
        "class_id": (q.get("clazzid") or q.get("classId") or [""])[0],
        "cpi": (q.get("cpi") or [""])[0],
        "enc": (q.get("enc") or [""])[0],
        "t": (q.get("t") or [""])[0],
        "is_stu_page": "mycourse/stu" in (url or ""),
    }


def build_work_list_url(*, course_id: str, class_id: str, cpi: str, stuenc: str,
                        work_enc: str, t: Optional[int] = None) -> str:
    return (f"{WORK_LIST_URL}?courseId={course_id}&classId={class_id}&cpi={cpi}&ut=s"
            f"&t={int(t if t is not None else time.time() * 1000)}"
            f"&stuenc={stuenc}&enc={work_enc}")


def build_studentcourse_url(*, course_id: str, class_id: str, cpi: str, enc: str, openc: str,
                            v: int = 2) -> str:
    """课程菜单里的「体验新版」入口（v=2&fromMiddle=1 才会返回带 #workEnc 的新版页）。"""
    return (f"{STUDENTCOURSE_URL}?courseId={course_id}&clazzid={class_id}&ut=s&enc={enc}&cpi={cpi}"
            f"&openc={openc}&v={v}&fromMiddle=1")


def parse_menu_params(url: str) -> dict:
    """从课程菜单项 URL 里取 enc / openc / cpi（这三个 MCP 本来就能纯 HTTP 拿到）。"""
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url or "").query)
    get = lambda k: (q.get(k) or [""])[0]          # noqa: E731
    return {"enc": get("enc"), "openc": get("openc"), "cpi": get("cpi"),
            "course_id": get("courseId") or get("courseid"),
            "class_id": get("classId") or get("clazzid")}


def classify_task_url(final_url: str) -> str:
    """task 链接最终落到哪儿：dowork（可作答）/ view（已截止只看详情）/ other。"""
    u = str(final_url or "")
    if "/mooc2/work/dowork" in u:
        return "dowork"
    if "/mooc2/work/view" in u:
        return "view"
    return "other"


class StuPageCache:
    """按**账号**隔离地缓存「新版学生课程页 URL」（每门课一次）。

    账号换了就整份作废（同 `store.menu_cache` 的规则）：这张票是站点按身份下发的，
    拿另一个账号的票据去用可能报错、也可能读到不该读的东西，不能当"全局配置"用。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.last_note = ""

    def _read_raw(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def load(self, account: str = "") -> dict:
        """返回 {课程键: 条目}；账号不符/旧格式一律当空（并记下原因）。"""
        raw = self._read_raw()
        if not raw:
            return {}
        owner = str(raw.get("account") or "")
        if not owner:
            self.last_note = "stu_pages.json 没有账号标记（旧格式），已作废，请重新 register_study_page"
            return {}
        if account and owner != str(account):
            self.last_note = (f"stu_pages.json 属于账号 {owner}，当前是 {account}，"
                              "已作废：请用当前账号重新 register_study_page")
            return {}
        entries = raw.get("entries")
        return entries if isinstance(entries, dict) else {}

    def put(self, *, course_id: str, class_id: str, stu_url: str, account: str = "") -> None:
        entries = self.load(account)
        entries[f"{course_id}:{class_id}"] = {"stu_url": stu_url, "saved_at": int(time.time())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"account": str(account), "saved_at": int(time.time()),
                                         "entries": entries}, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def get(self, *, course_id: str, class_id: str = "", account: str = "") -> str:
        entries = self.load(account)
        if class_id and f"{course_id}:{class_id}" in entries:
            return entries[f"{course_id}:{class_id}"].get("stu_url", "")
        for v in entries.values():                    # class_id 未知时按课程号兜底
            info = parse_stu_url(v.get("stu_url", ""))
            if info.get("course_id") == str(course_id):
                return v.get("stu_url", "")
        return ""


def detect_work_kind(client: httpx.Client, task_url: str) -> str:
    """打开 task 链接看最终落到 dowork（可作答）还是 view（已截止）。"""
    try:
        resp = client.get(task_url, timeout=25)
    except httpx.HTTPError:
        return "error"
    kind = classify_task_url(str(resp.url))
    if kind == "other":                      # 客户端不跟随重定向时看 Location
        kind = classify_task_url(resp.headers.get("location", ""))
    return kind


def _finish(client: httpx.Client, *, course_id: str, class_id: str, cpi: str, stuenc: str,
            work_enc: str, source: str) -> dict:
    notes: list[str] = []
    list_url = build_work_list_url(course_id=course_id, class_id=class_id, cpi=cpi,
                                   stuenc=stuenc, work_enc=work_enc)
    r = client.get(list_url, timeout=25)
    works = parse_work_encs(r.text or "")
    if not works:
        notes.append(f"作业列表页没解析到作业（HTTP {r.status_code}，{len(r.text or '')} 字节）；"
                     "通常是票据过期，或这门课在新版页面里没有作业")
    return {"work_enc": work_enc, "works": works, "notes": notes,
            "source": source, "list_url": list_url}


def discover_course_encs(client: httpx.Client, *, course_id: str, class_id: str, cpi: str,
                         enc: str = "", openc: str = "", stu_url: str = "") -> dict:
    """跑 ①→② 拿每个作业的 enc。

    · 首选：课程菜单的 `enc`+`openc`（纯 HTTP，零用户输入）
    · 兜底：用户直接给的新版课程页 `stu_url`（`register_study_page`）
    """
    notes: list[str] = []
    if enc and openc:
        page = client.get(build_studentcourse_url(course_id=course_id, class_id=class_id, cpi=cpi,
                                                  enc=enc, openc=openc), timeout=25)
        work_enc = parse_work_enc(page.text or "")
        if work_enc:
            out = _finish(client, course_id=course_id, class_id=class_id, cpi=cpi,
                          stuenc=enc, work_enc=work_enc, source="course-menu")
            out["notes"] = notes + out["notes"]
            return out
        notes.append(f"「体验新版」课程页没解析到 #workEnc（HTTP {page.status_code}，"
                     f"{len(page.text or '')} 字节）；改用已注册的新版课程页兜底")
    if not stu_url:
        return {"work_enc": "", "works": {}, "notes": notes + ["没有可用的课程票据"], "source": ""}
    stu = client.get(stu_url, timeout=25)
    work_enc = parse_work_enc(stu.text or "")
    if not work_enc:
        notes.append(f"新版课程页没解析到 #workEnc（HTTP {stu.status_code}，{len(stu.text or '')} 字节）；"
                     "该 URL 可能已失效")
        return {"work_enc": "", "works": {}, "notes": notes, "source": "registered-stu-url",
                "stu_url": stu_url}
    out = _finish(client, course_id=course_id, class_id=class_id, cpi=cpi,
                  stuenc=parse_stu_url(stu_url).get("enc", "") or enc, work_enc=work_enc,
                  source="registered-stu-url")
    out["notes"] = notes + out["notes"]
    out["stu_url"] = stu_url
    return out
