"""作业列表抓取 + 状态映射。

解析规则与 chaoxing_mcp 保持一致（用户决定）：只认「待做」为未完成。
但额外保留 status_raw / raw_spans，便于拿真实数据核查文案到底长什么样。

参考实现的关键步骤（这里逐条对齐）：
  1. enc  = re.search(r"&enc=([a-z0-9]{32})", html)
     优先  从 <script> 里 $(".inspectTask").click 那段抓 [?&]enc=([a-z0-9]{32})
  2. openc = re.search(r"openc=([a-zA-Z0-9]+)", menu_url)
  3. 逐 <li>：a.inspectTask（退化到任意 a）
       workId       = a["data"]  / a["data-workid"]
       workAnswerId = a["data2"] / a["data-answerid"]
       文案扫 span.pt5（或 span.time）
  4. 状态码：待做→0 待批阅→1 已完成→2 其它→3(UNKNOWN)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from .courses import MOOC, Course, FetchError, MenuItem, _guard, absolute

# 与 chaoxing_mcp 完全一致的状态规则表（用户决定保留）
STATUS_RULES: tuple[tuple[str, int], ...] = (
    ("待做", 0),
    ("待批阅", 1),
    ("已完成", 2),
)
STATUS_UNKNOWN = 3
STATUS_DONE_TEXTS = ("已完成", "已批阅", "已交", "已提交", "已结束", "已过期")


@dataclass
class Homework:
    work_id: str
    name: str
    course_id: str
    class_id: str
    cpi: str
    status_raw: str = ""
    status_num: int = STATUS_UNKNOWN
    work_answer_id: str = ""
    start_time: str = ""
    end_time: str = ""
    enc: str = ""
    openc: str = ""
    url: str = ""
    detail_url: str = ""          # 详情页（形态 B 直接用列表页 href；形态 A 由 url() 拼）
    raw_spans: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_unfinished_strict(self) -> bool:
        """chaoxing_mcp 口径：只有「待做」算未完成。"""
        return self.status_num == 0

    @property
    def is_not_done(self) -> bool:
        """宽松口径（仅用于诊断对比）：文案里没有完成态字样就算未做完。"""
        return not any(k in (self.status_raw or "") for k in STATUS_DONE_TEXTS)


def classify_status(text: str) -> int:
    """按 chaoxing_mcp 的规则表映射状态码；不在表内一律 UNKNOWN(3)。"""
    for key, code in STATUS_RULES:
        if key in (text or ""):
            return code
    return STATUS_UNKNOWN


def _extract_enc(html: str, fallback: str = "") -> str:
    """作业详情专用 enc：优先脚本里的，其次 HTML 里的 &enc=。"""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        src = script.string or ""
        if '$(".inspectTask").click' in src:
            m = re.search(r"[?&]enc=([a-z0-9]{32})", src, re.I)
            if m:
                return m.group(1)
    m = re.search(r"&enc=([a-z0-9]{32})", html)
    return m.group(1) if m else fallback


def _extract_openc(menu_url: str) -> str:
    m = re.search(r"openc=([a-zA-Z0-9]+)", menu_url or "")
    return m.group(1) if m else ""


def build_homework_url(item: Homework, *, base: str = MOOC) -> str:
    import urllib.parse

    params = {
        "courseId": item.course_id,
        "classId": item.class_id,
        "workId": item.work_id,
        "workAnswerId": item.work_answer_id or "0",
        "isdisplaytable": "2",
        "mooc": "1",
        "enc": item.enc,
        "workSystem": "0",
        "cpi": item.cpi,
        "openc": item.openc,
    }
    return f"{base}/mooc-ans/work/doHomeWorkNew?" + urllib.parse.urlencode(params)


def _query_param(url: str, key: str) -> str:
    import urllib.parse

    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    vals = q.get(key) or [""]
    return vals[0]


def parse_homework_list(html: str, course: Course, menu_url: str) -> list[Homework]:
    """解析作业列表页。

    容器：div.ulDiv li（实测 5/5 真项都在这里；导航的 li 在 ulDiv 之外，天然排除）
    真项判据：li 内有 div.titTxt（含 a[title] + span.pt5 状态）

    两种形态（实测）：
      A 可编辑：<a class="inspectTask" data=workId data2=workAnswerId data3=1 title=名称>
                → 详情需自己拼 /mooc-ans/work/doHomeWorkNew?...
      B 已批阅/已完成：<li class="lookLi"> 内 <a href="/mooc-ans/work/selectWorkQuestionYiPiYue?
                courseId=..&classId=..&workId=..&workAnswerId=..&isdisplaytable=2&mooc=1&ut=s&enc=..&workSystem=0&cpi=..">
                → href 本身就是详情页；无 data 属性
    """
    soup = BeautifulSoup(html, "html.parser")
    enc = _extract_enc(html)
    openc = _extract_openc(menu_url)
    out: list[Homework] = []

    for li in soup.select("div.ulDiv li"):
        tit = li.find("div", class_="titTxt")
        if tit is None:
            continue
        a = tit.find("a")
        if a is None:
            continue
        name = (a.get("title") or a.get_text(strip=True) or "").strip()
        if not name:
            continue

        href = a.get("href", "") or ""
        work_id = str(a.get("data", "") or "")
        work_answer_id = str(a.get("data2", "") or "")
        detail_url = ""
        if href and not href.startswith("javascript:"):
            detail_url = absolute(MOOC, href)
            work_id = work_id or _query_param(detail_url, "workId")
            work_answer_id = work_answer_id or _query_param(detail_url, "workAnswerId")
            enc = _query_param(detail_url, "enc") or enc
            openc = _query_param(detail_url, "openc") or openc

        spans = li.find_all("span", class_="pt5")
        span_texts = [sp.get_text(strip=True) for sp in spans]
        start_time = end_time = status_raw = ""
        for sp in spans:
            label_el = sp.find("span", class_="fl")
            label = label_el.get_text(strip=True) if label_el else ""
            strong = sp.find("strong")
            value = strong.get_text(strip=True) if strong else ""
            if not value:
                value = sp.get_text(strip=True)
                if label and value.startswith(label):
                    value = value[len(label):].strip()
            if "开始" in label:
                start_time = value
            elif "截止" in label or "结束" in label:
                end_time = value
            elif "状态" in label:
                status_raw = value

        hw = Homework(
            work_id=work_id, name=name,
            course_id=course.course_id, class_id=course.class_id, cpi=course.cpi,
            status_raw=status_raw, status_num=classify_status(status_raw),
            work_answer_id=work_answer_id,
            start_time=start_time, end_time=end_time,
            enc=enc, openc=openc,
            detail_url=detail_url,
            raw_spans=tuple(span_texts),
        )
        hw.url = detail_url or build_homework_url(hw)
        out.append(hw)
    return out


LIST_MARKER = "ulDiv"          # 真·作业列表页一定含这个容器（空列表页也有）


def fetch_homework_list(client: httpx.Client, course: Course, menu: MenuItem,
                        *, dump_html_to: Optional[str] = None) -> tuple[list[Homework], str]:
    """抓取某门课的作业列表页并解析。返回 (作业列表, 原始 HTML)。

    关键：**区分"真的没有作业"和"拿到的不是作业列表页"**。
      · 页面含 ulDiv（可能是空的）→ 空列表 = 真的没有作业
      · 页面不含 ulDiv → 抛 FetchError（风控页 / 跳转页 / 结构改版），绝不返回空列表
    实测踩过：换账号后第一次请求拿到 HTTP 200 的风控页，被当成"0 条作业"。
    """
    menu_url = absolute(MOOC, menu.effective_url or menu.data or menu.href)
    resp = client.get(menu_url, timeout=20)
    _guard(resp, f"获取作业列表({course.course_name})")
    html = resp.text
    if dump_html_to:
        from pathlib import Path
        p = Path(dump_html_to)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")

    items = parse_homework_list(html, course, menu_url)
    if not items and LIST_MARKER not in html:
        import re as _re
        head = _re.sub(r"\s+", " ", html).strip()[:160]
        raise FetchError(
            f"获取作业列表({course.course_name}): 返回的不是作业列表页"
            f"（HTTP {resp.status_code}，{len(html)} 字节，final={resp.url}）"
            f"，可能命中风控/验证或页面改版；正文开头: {head!r}"
        )
    return items, html


def status_distribution(items: list[Homework]) -> dict[str, int]:
    """把原始状态文案做成分布，用来判断规则表是否还跟得上线上文案。"""
    dist: dict[str, int] = {}
    for h in items:
        key = h.status_raw or "(空)"
        dist[key] = dist.get(key, 0) + 1
    return dict(sorted(dist.items(), key=lambda kv: -kv[1]))
