"""会话与 cookie 持久化。

与参考实现（chaoxing_mcp）的三处关键区别：
  1. 不用 pickle：cookie 存成 JSON，可读、可审计、无反序列化执行风险；
  2. 保留 expires：过期 cookie 在加载时就被丢弃，避免"cookie 存在即已登录"的假成功；
  3. 原子写入：先写临时文件再 os.replace，写坏不会留下半个文件。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import httpx

# 学习通 cookie 都挂在 .chaoxing.com 上；部署到别的域（或本地 mock）时改这个常量。
DEFAULT_COOKIE_DOMAIN = ".chaoxing.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

__all__ = [
    "DEFAULT_COOKIE_DOMAIN", "Cookie", "StateFile", "build_client", "load_cookies",
    "load_state", "save_cookies",
    "cookies_from_client", "apply_cookies", "parse_cookie_blob",
]


@dataclass
class Cookie:
    name: str
    value: str
    domain: str = ".chaoxing.com"
    path: str = "/"
    expires: Optional[float] = None   # epoch 秒；None 表示会话 cookie
    http_only: bool = False
    secure: bool = False
    same_site: str = ""

    @property
    def expired(self) -> bool:
        return self.expires is not None and self.expires <= time.time()


def build_client(cookies: Iterable[Cookie] = (), *, insecure: bool = False, timeout: float = 10.0) -> httpx.Client:
    """构造 httpx 客户端。

    默认走系统证书校验（不学参考实现的 verify=False）；只有显式开启 insecure
    才放宽 TLS，用来对付个别老域名（可配合 SECLEVEL 降低）。
    """
    client = httpx.Client(
        headers={
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        follow_redirects=True,
        timeout=httpx.Timeout(timeout, connect=5.0),
        verify=not insecure,
    )
    apply_cookies(client, cookies)
    return client


def apply_cookies(client: httpx.Client, cookies: Iterable[Cookie]) -> int:
    """按 domain/path 回填（只 set(name,value) 会挂到默认域上，跨域带不出去）。"""
    n = 0
    for c in cookies:
        if c.expired:
            continue
        client.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        n += 1
    return n


def cookies_from_client(client: httpx.Client) -> list[Cookie]:
    out: list[Cookie] = []
    for c in client.cookies.jar:
        expires = None
        if getattr(c, "expires", None):
            expires = float(c.expires)
        out.append(Cookie(
            name=c.name, value=c.value,
            domain=c.domain or "", path=c.path or "/",
            expires=expires,
            http_only=bool(c.has_nonstandard_attr("HttpOnly")) if hasattr(c, "has_nonstandard_attr") else False,
            secure=bool(c.secure),
            same_site=getattr(c, "same_site", "") or "",
        ))
    return out


@dataclass
class StateFile:
    """落盘的会话状态。account 记录它属于哪个账号 —— 换账号时必须靠它识别并忽略旧会话。"""

    cookies: list[Cookie] = field(default_factory=list)
    account: str = ""
    saved_at: int = 0


def load_state(path: Path | str) -> StateFile:
    """读 cookie 状态文件；过期的 cookie 直接丢弃（"假成功"的根治点）。"""
    p = Path(path)
    if not p.is_file():
        return StateFile()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return StateFile()
    items = raw.get("cookies", raw) if isinstance(raw, dict) else raw
    cookies: list[Cookie] = []
    known = {f for f in Cookie.__dataclass_fields__}
    for item in items or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        cookies.append(Cookie(**{k: v for k, v in item.items() if k in known}))
    account = str(raw.get("account", "") if isinstance(raw, dict) else "")
    saved_at = int(raw.get("saved_at", 0) if isinstance(raw, dict) else 0)
    return StateFile(cookies=[c for c in cookies if not c.expired],
                     account=account, saved_at=saved_at)


def load_cookies(path: Path | str) -> list[Cookie]:
    """兼容旧调用：只取 cookie 列表（不做账号校验，校验在 ensure_session 里做）。"""
    return load_state(path).cookies


def save_cookies(cookies: Iterable[Cookie], path: Path | str, *, account: str = "") -> Path:
    """原子写入 JSON（含 expires + 所属账号），按 cookie 名排序保证 diff 稳定。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "account": account,
        "saved_at": int(time.time()),
        "cookies": [asdict(c) for c in sorted(cookies, key=lambda c: (c.domain, c.name))],
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def parse_cookie_blob(blob: str, default_domain: str | None = None) -> list[Cookie]:
    """解析 XUEXITONG_COOKIE：支持 `a=1; b=2` 或每行一条（浏览器扩展复制出来的格式）。

    纯 name=value 格式不带 domain/path，这里按学习通默认域补齐 —— 若目标不是
    .chaoxing.com（例如本地 mock 或私有化部署），传 default_domain 覆盖。
    """
    cookies: list[Cookie] = []
    for line in blob.replace("\r", "\n").split("\n"):
        for part in line.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if name:
                cookies.append(Cookie(name=name, value=value,
                                       domain=default_domain or DEFAULT_COOKIE_DOMAIN))
    return cookies
