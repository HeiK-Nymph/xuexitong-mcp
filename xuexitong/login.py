"""登录 / 探测 / 会话编排。

登录协议来源（实测自 passport2-static.chaoxing.com/js/fanya/login.js）：
    POST https://passport2.chaoxing.com/fanyalogin
    fid=-1  uname=AES(账号)  password=AES(密码)  refer=...  t=true
    forbidotherlogin=0  validate=  doubleFactorLogin=0  independentId=0  independentNameId=0
    响应 JSON：status=true 即成功（url 为跳转地址）；失败读 msg2。
  · t=true 表示"密码已加密"，所以明文 + t=true 是自相矛盾的组合（参考实现就这么发的）
  · containTwoFactorLogin=true → 需要双因子；weakpwd=true → 需要改弱密码
  · 登录页 needVcode=1 → 需要先过滑动验证，此时不要静默重试

编排策略（"最优路径"）：
    磁盘 cookie → 探测有效即复用（零登录）
    → env 里的 cookie 串 → 探测有效即复用
    → 账号密码（AES）→ 登录成功后落盘带 expires 的 cookie
    → 都没有 → 明确报错
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

from .config import AuthConfig
from .crypto import encrypt
from .session import (
    Cookie, apply_cookies, build_client, cookies_from_client,
    load_state, parse_cookie_blob, save_cookies,
)

PASSPORT = "https://passport2.chaoxing.com"
SSO = "https://sso.chaoxing.com"
MOOC_API = "https://mooc1-api.chaoxing.com"
REFER = "https://i.chaoxing.com"

__all__ = [
    "LoginOutcome", "LoginResult", "SessionInfo", "ensure_session", "login_with_password",
    "is_login_location",
    "probe", "probe_business", "read_need_vcode", "fetch_login_page",
]


class LoginOutcome(str, Enum):
    OK = "OK"
    BAD_CREDENTIALS = "BAD_CREDENTIALS"
    NEED_HUMAN_VERIFICATION = "NEED_HUMAN_VERIFICATION"
    NEED_2FA = "NEED_2FA"
    WEAK_PASSWORD = "WEAK_PASSWORD"
    HTTP_ERROR = "HTTP_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass
class LoginResult:
    outcome: LoginOutcome
    message: str = ""
    uid: str = ""
    name: str = ""
    redirect: str = ""
    status_code: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome is LoginOutcome.OK


@dataclass
class SessionInfo:
    ok: bool
    source: str = "none"          # state-cookie | env-cookie | password | none
    uid: str = ""
    name: str = ""
    error_code: str = ""
    message: str = ""
    elapsed_ms: int = 0
    cookie_count: int = 0
    client: Optional[httpx.Client] = None

    def summary(self) -> str:
        if self.ok:
            return (f"会话可用（来源 {self.source}）：uid={self.uid or '未知'} "
                    f"name={self.name or '未知'} cookie={self.cookie_count} 张 "
                    f"耗时 {self.elapsed_ms}ms")
        return f"会话不可用（{self.error_code}）：{self.message}"


# ---------------------------------------------------------------- 基础步骤

def fetch_login_page(client: httpx.Client) -> str:
    """预登录：拿初始 cookie，并顺便读 needVcode。"""
    resp = client.get(f"{PASSPORT}/login", params={"newversion": "true"}, timeout=10)
    resp.raise_for_status()
    return resp.text


def read_need_vcode(client: httpx.Client) -> bool:
    """登录页 needVcode=1 表示必须先过滑动验证（官方会 loadSlide 后再提交）。"""
    import re

    html = fetch_login_page(client)
    m = re.search(r'id="needVcode"[^>]*value="([^"]*)"', html)
    return bool(m and m.group(1).strip() == "1")


def build_login_data(username: str, password: str, *, encrypt_payload: bool = True,
                     t: str = "true", fid: str = "-1") -> dict:
    """构造 fanyalogin 请求体。encrypt_payload=False 用于复现参考实现的错误组合。"""
    uname = encrypt(username) if encrypt_payload else username
    pwd = encrypt(password) if encrypt_payload else password
    return {
        "fid": fid,
        "uname": uname,
        "password": pwd,
        "refer": REFER,
        "t": t,
        "forbidotherlogin": "0",
        "validate": "",
        "doubleFactorLogin": "0",
        "independentId": "0",
        "independentNameId": "0",
    }


def _classify(payload: dict, status_code: int) -> LoginResult:
    if payload.get("status") is True:
        return LoginResult(
            outcome=LoginOutcome.OK,
            uid=str(payload.get("uid", "") or ""),
            name=str(payload.get("name", "") or ""),
            redirect=str(payload.get("url", "") or ""),
            status_code=status_code, raw=payload,
        )
    if payload.get("containTwoFactorLogin"):
        return LoginResult(LoginOutcome.NEED_2FA, "账号开启了双因子验证，需要人工完成",
                           status_code=status_code, raw=payload)
    if payload.get("weakpwd"):
        return LoginResult(LoginOutcome.WEAK_PASSWORD, "密码被判定为弱密码，需先在学习通改密",
                           status_code=status_code, raw=payload)
    msg = str(payload.get("msg2") or payload.get("msg") or "").strip()
    if "验证码" in msg:
        return LoginResult(LoginOutcome.NEED_HUMAN_VERIFICATION, msg, status_code=status_code, raw=payload)
    if msg:
        return LoginResult(LoginOutcome.BAD_CREDENTIALS, msg, status_code=status_code, raw=payload)
    return LoginResult(LoginOutcome.UNKNOWN, f"未知响应: {json.dumps(payload, ensure_ascii=False)[:200]}",
                       status_code=status_code, raw=payload)


def login_with_password(client: httpx.Client, username: str, password: str, *,
          encrypt_payload: bool = True, t: str = "true", pre_login: bool = False) -> LoginResult:
    """执行一次 fanyalogin。失败原因按服务端的 msg2 分类，不再一锅烩。"""
    if pre_login:
        try:
            fetch_login_page(client)
        except httpx.HTTPError as e:
            return LoginResult(LoginOutcome.HTTP_ERROR, f"预登录失败: {e}")
    data = build_login_data(username, password, encrypt_payload=encrypt_payload, t=t)
    try:
        resp = client.post(f"{PASSPORT}/fanyalogin", data=data, timeout=10)
    except httpx.HTTPError as e:
        return LoginResult(LoginOutcome.HTTP_ERROR, f"登录请求失败: {e}")
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text or ""
        if "恭喜您" in text or "登录成功" in text:
            # 老式 HTML 响应，没有 JSON；靠 cookie 判断
            return LoginResult(LoginOutcome.OK, "HTML 响应视为成功", status_code=resp.status_code)
        return LoginResult(LoginOutcome.UNKNOWN,
                           f"非 JSON 响应 HTTP {resp.status_code}: {text[:120]}",
                           status_code=resp.status_code)
    if not isinstance(payload, dict):
        return LoginResult(LoginOutcome.UNKNOWN, "响应不是对象", status_code=resp.status_code)
    return _classify(payload, resp.status_code)


PROTECTED_URL = "https://i.chaoxing.com/base"
LOGIN_MARK = "passport2.chaoxing.com/login"


def _log(message: str) -> None:
    """日志只能写 stderr（stdout 在 MCP 下是协议通道）。"""
    print(f"[xuexitong.login] {message}", file=sys.stderr, flush=True)


def _uid_of_blob(blob: str) -> str:
    """从 XUEXITONG_COOKIE 串里解析 _uid/UID，用于识别这份 cookie 属于谁。"""
    for c in parse_cookie_blob(blob):
        if c.name in ("_uid", "UID") and c.value:
            return c.value
    return ""


def is_login_location(location: str) -> bool:
    """Location 是否指向登录页。

    线上实测形态（绝对 URL）：
        https://passport2.chaoxing.com/login?fid=12&refer=http%3A%2F%2Fi.chaoxing.com%2Fbase%3Fnull&space=2
    同时兼容相对路径（/login?...），避免部署变化时把"未登录"误判成"已登录"。
    """
    loc = (location or "").strip().lower()
    if not loc:
        return False
    if "passport2.chaoxing.com/login" in loc:
        return True
    path = loc.split("?", 1)[0]
    return path.endswith("/login") or path.endswith("/login.do")


def redirected_to_login(client: httpx.Client, url: str | None = None) -> bool:
    """未登录时学习通会把受保护页面 302 到 passport2 登录页（实测特征）。

    这是最可靠的判据：它由服务端明确表态，而不是"cookie 名字是否存在"。
    网络异常按"不可用"处理（保守，避免假成功）。
    """
    try:
        resp = client.get(url or PROTECTED_URL, follow_redirects=False, timeout=10)
    except httpx.HTTPError:
        return True
    if resp.status_code in (301, 302, 303, 307, 308):
        return is_login_location(resp.headers.get("location", "") or "")
    return False


def probe(client: httpx.Client) -> tuple[bool, str, str, dict]:
    """会话有效性探测。

    判定：受保护页面没被踢回登录页 = 会话可用（主判据，可靠）；
          再用 SSO 的 userLogin4UAP 尽力补充 uid / name。
    注意：SSO 未登录时返回 {"result":"0","errorMsg":"...服务异常...[50001]"}（HTTP 200），
          所以它不能单独当判据，只能做补充。
    """
    detail: dict = {}
    if redirected_to_login(client):
        detail["reason"] = "redirected-to-login"
        return False, "", "", detail
    uid = name = ""
    try:
        resp = client.get(f"{SSO}/apis/login/userLogin4UAP.do", timeout=8)
        payload = resp.json()
        detail["sso"] = payload
        msg = payload.get("msg") if isinstance(payload, dict) else None
        if isinstance(msg, dict):
            uid = str(msg.get("uid", "") or "")
            name = str(msg.get("name", "") or "")
        elif str(payload.get("result", "")) == "1":
            uid = str(payload.get("uid", "") or "")
    except (httpx.HTTPError, ValueError) as e:
        detail["sso_error"] = str(e)
    if not uid:
        uid = uid_from_cookies(client)
        if uid:
            detail["uid_source"] = "cookie"
    return True, uid, name, detail


def uid_from_cookies(client: httpx.Client) -> str:
    """cookie 里的 _uid / UID 就是用户 id（参考实现也这么做）。"""
    for name in ("_uid", "UID"):
        for c in client.cookies.jar:
            if c.name == name and c.value:
                return str(c.value)
    return ""


def probe_business(client: httpx.Client) -> tuple[bool, int]:
    """更硬的探测：能否真的拉到课程列表（业务接口也认这套 cookie）。"""
    if redirected_to_login(client, f"{MOOC_API}/mycourse/backclazzdata"):
        return False, 0
    try:
        resp = client.get(f"{MOOC_API}/mycourse/backclazzdata", timeout=10)
    except httpx.HTTPError:
        return False, 0
    if resp.status_code != 200:
        return False, 0
    try:
        data = resp.json()
    except ValueError:
        return False, 0
    if not isinstance(data, dict):
        return False, resp.status_code
    n = 0
    for channel in data.get("channelList") or []:
        content = channel.get("content") or {}
        if isinstance(content, dict):
            n += len((content.get("course") or {}).get("data") or [])
    return n > 0, n


# ---------------------------------------------------------------- 编排

def ensure_session(cfg: AuthConfig, *, client: Optional[httpx.Client] = None,
                   force_login: bool = False, verify_business: bool = False) -> SessionInfo:
    """最优路径编排：磁盘 cookie → env cookie → 账号密码（AES）→ 报错。"""
    started = time.perf_counter()
    own_client = client is None
    if client is None:
        client = build_client(insecure=cfg.insecure)

    def _done(ok: bool, **kw) -> SessionInfo:
        kw.setdefault("client", client if not own_client else client)
        return SessionInfo(ok=ok, elapsed_ms=int((time.perf_counter() - started) * 1000), **kw)

    # 1) 磁盘 cookie —— 必须确认它属于当前配置的账号，否则换账号会静默沿用旧会话
    expected = cfg.username.strip()
    if not expected and cfg.has_cookie:
        expected = _uid_of_blob(cfg.cookie_blob)
    if not force_login:
        state = load_state(cfg.state_path)
        cookies = state.cookies
        if cookies and expected and state.account != expected:
            detail = f"cookie 属于账号 {state.account or '未知'}，当前配置 {expected}，已忽略"
            _log(detail)
            cookies = []
        elif cookies and expected and not state.account:
            _log(f"cookie 文件没有账号标记，无法确认归属（当前 {expected}），已忽略")
            cookies = []
        if cookies:
            n = apply_cookies(client, cookies)
            valid, uid, name, raw = probe(client)
            if valid:
                extra = ""
                if verify_business:
                    b_ok, b_n = probe_business(client)
                    if not b_ok:
                        extra = "（但业务接口不可用）"
                return _done(True, source="state-cookie", uid=uid, name=name,
                             cookie_count=n, message=extra)
            # 失效：删掉，避免下次又被当成"已登录"
            try:
                os.remove(cfg.state_path)
            except OSError:
                pass

    # 2) env 里的 cookie 串
    if cfg.has_cookie and not force_login:
        blob = parse_cookie_blob(cfg.cookie_blob)
        if blob:
            n = apply_cookies(client, blob)
            valid, uid, name, raw = probe(client)
            if valid:
                save_cookies(cookies_from_client(client), cfg.state_path, account=expected or uid_from_cookies(client))
                return _done(True, source="env-cookie", uid=uid, name=name, cookie_count=n)

    # 3) 账号密码
    if not cfg.has_password:
        return _done(False, source="none", error_code="NO_CREDENTIAL",
                     message=("未配置可用凭据：请在 MCP 环境变量里设置 "
                              "XUEXITONG_COOKIE（优先）或 XUEXITONG_USERNAME + XUEXITONG_PASSWORD"))

    if read_need_vcode(client):
        return _done(False, source="none", error_code="NEED_HUMAN_VERIFICATION",
                     message="学习通要求先完成滑动验证（needVcode=1）。请在浏览器里登录一次，"
                             "或改用 XUEXITONG_COOKIE 提供 cookie；此处不静默重试，以免加剧风控。")

    result = login_with_password(client, cfg.username, cfg.password, pre_login=False)
    if not result.ok:
        return _done(False, source="none", error_code=result.outcome.value, message=result.message)

    valid, uid, name, _ = probe(client)
    if not valid:
        # 登录接口说成功但探测不过：宁可报错，不要留一个"看起来已登录"的会话
        uid, name = result.uid, result.name
    cookies = cookies_from_client(client)
    saved = save_cookies(cookies, cfg.state_path, account=expected or uid_from_cookies(client))
    return _done(True, source="password", uid=uid or result.uid, name=name or result.name,
                 cookie_count=len(cookies),
                 message="" if valid else f"登录成功但探测未确认（cookie 已存 {saved}）")
