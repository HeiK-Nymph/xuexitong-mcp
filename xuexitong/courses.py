"""课程列表 + 课程菜单。

端点与解析路径来自 chaoxing_mcp（现有可用实现）：
    课程列表   GET https://mooc1-api.chaoxing.com/mycourse/backclazzdata
               channelList[] → content{} → course{} → data[] → {id, name}
               content.id = clazzid/classid，content.cpi = cpi
    课程菜单   GET https://mooc1-2.chaoxing.com/visit/stucoursemiddle?courseid=&clazzid=&vc=1&cpi=
               "作业"项的 href 常是 javascript:;，真实 URL 在 data 属性里

与参考实现的区别：抓不到东西一律抛 FetchError，不再返回空列表（空列表 = 假阴性）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup

MOOC = "https://mooc1-2.chaoxing.com"
MOOC_API = "https://mooc1-api.chaoxing.com"


class FetchError(RuntimeError):
    """抓取失败（与"确实没有数据"区分开）。"""


class SessionExpired(FetchError):
    """会话失效：请求被 302 回登录页。"""


class AntispiderError(FetchError):
    """命中反爬（202 / antispiderShowVerify），且无法自动通过。"""


@dataclass
class Course:
    course_id: str
    course_name: str
    class_id: str
    clazz_id: str
    cpi: str
    url: str

    def __post_init__(self) -> None:
        if not self.clazz_id:
            self.clazz_id = self.class_id


@dataclass
class MenuItem:
    name: str
    href: str = ""
    data: str = ""
    effective_url: str = ""


def _looks_like_login(resp: httpx.Response) -> bool:
    """响应是否意味着"会话失效"：被 302 到登录页，或最终落在登录页。"""
    from .login import is_login_location

    if resp.status_code in (301, 302, 303, 307, 308):
        return is_login_location(resp.headers.get("location", "") or "")
    return "passport2.chaoxing.com/login" in str(resp.url)


ANTISPIDER_MARK = "antispiderShowVerify"


def _guard(resp: httpx.Response, what: str) -> None:
    """统一的失败分类：会话失效 / 反爬 / HTTP 错误，全部显式抛错。

    注意：学习通的风控验证页可能是 **HTTP 200**，所以除了状态码与 URL，
    还要看响应体里有没有 antispider 标记（只检查前 4KB，避免长正文开销）。
    """
    if resp.status_code == 200 and ANTISPIDER_MARK in (resp.text or "")[:4096]:
        raise AntispiderError(
            f"{what}: 命中学习通反爬验证页（HTTP 200，URL {resp.url}）。"
            "需要人工过一次验证码，或降低请求频率。"
        )
    if _looks_like_login(resp):
        raise SessionExpired(f"{what}: 会话失效，被重定向到登录页（{resp.url}）")
    url = str(resp.url)
    if resp.status_code == 202 or ANTISPIDER_MARK in url:
        raise AntispiderError(
            f"{what}: 命中学习通反爬（HTTP {resp.status_code} / {url}）。"
            "需要人工过一次图形验证码，或降低请求频率。"
        )
    if resp.status_code != 200:
        raise FetchError(f"{what}: HTTP {resp.status_code}")


def get_course_list(client: httpx.Client, base: str = MOOC_API) -> list[Course]:
    """课程列表（JSON 接口，无参数）。"""
    resp = client.get(f"{base}/mycourse/backclazzdata", timeout=15)
    _guard(resp, "获取课程列表")
    try:
        data = resp.json()
    except ValueError as e:
        raise FetchError(f"获取课程列表: 响应不是 JSON（HTTP {resp.status_code}）：{resp.text[:160]!r}") from e
    if not isinstance(data, dict):
        raise FetchError(f"获取课程列表: 响应结构异常 {type(data).__name__}")

    courses: list[Course] = []
    for channel in data.get("channelList") or []:
        content = channel.get("content") if isinstance(channel, dict) else None
        if not isinstance(content, dict):
            continue
        course_block = content.get("course") or {}
        items = course_block.get("data") if isinstance(course_block, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            course_id = str(item.get("id", "") or "")
            if not course_id:
                continue
            class_id = str(content.get("id", "") or "")
            cpi = str(content.get("cpi", "") or "")
            url = (f"{MOOC}/visit/stucoursemiddle"
                   f"?courseid={course_id}&clazzid={class_id}&vc=1&cpi={cpi}")
            courses.append(Course(
                course_id=course_id,
                course_name=str(item.get("name", "") or ""),
                class_id=class_id,
                clazz_id=class_id,
                cpi=cpi,
                url=url,
            ))
    return courses


def _class_contains(tag, cls: str) -> bool:
    classes = tag.get("class") or []
    return cls in classes


def get_course_menu(client: httpx.Client, course: Course) -> tuple[list[MenuItem], str]:
    """课程菜单。返回 (菜单项, 命中策略名)。

    策略 1（与参考实现一致）：`.navshow li / .course-nav li / .menu-list li` 里的 <a>
    策略 2（兜底）：全页扫 <a>，挑 data 属性里带 /work/ 或 getAllWork 的
    """
    resp = client.get(course.url, timeout=15)
    _guard(resp, f"获取课程菜单({course.course_name})")
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    items: list[MenuItem] = []
    for li in soup.select(".navshow li, .course-nav li, .menu-list li"):
        a = li.find("a")
        if not a:
            continue
        href = a.get("href", "") or ""
        data_url = a.get("data", "") or ""
        effective = data_url if (href.startswith("javascript:") or href == "#") and data_url else href
        items.append(MenuItem(name=a.get_text(strip=True), href=href, data=data_url, effective_url=effective))
    if items:
        return items, "selectors"

    for a in soup.find_all("a"):
        data_url = a.get("data", "") or ""
        href = a.get("href", "") or ""
        if "/work/" in data_url or "getAllWork" in data_url or "/work/" in href or "getAllWork" in href:
            effective = data_url or href
            items.append(MenuItem(name=a.get_text(strip=True), href=href, data=data_url, effective_url=effective))
    if items:
        return items, "fallback_scan"
    raise FetchError(f"课程菜单({course.course_name}): 一个菜单项都没解析到（页面 {len(html)} 字节），"
                     "可能是页面结构变化或未真正进入课程页")


def find_homework_menu(items: list[MenuItem]) -> Optional[MenuItem]:
    """找"作业"入口（与参考实现一致的中文包含匹配）。"""
    for item in items:
        if "作业" in item.name:
            return item
    return None


def absolute(base: str, url: str) -> str:
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return url
