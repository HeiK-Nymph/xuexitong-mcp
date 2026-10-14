"""本地 mock 学习通服务器 —— 用真实协议形状（非真实凭据）验证编排逻辑。

模拟行为（均来自前面的实测）：
  GET  /login?newversion=true          → HTML，含 needVcode
  POST /fanyalogin                     → {"status":bool,"msg2":...,"uid":...,"url":...}
                                         成功时下发会话 cookie（含一张 Max-Age 持久 cookie）
  GET  /apis/login/userLogin4UAP.do    → 已登录 {"result":"1","msg":{"uid","name"}}；否则 50001
  GET  /base                           → 未登录 302 → /login；已登录 200
  GET  /mycourse/backclazzdata         → 未登录 302；已登录 {"channelList":[...]}
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERNAME = "test_user"
PASSWORD = "test_password_123"


class MockState:
    def __init__(self) -> None:
        self.need_vcode = False
        self.login_posts: list[dict] = []      # 记录每次 fanyalogin 请求体
        self.login_ok = False
        self.token = ""
        self.fail_msg = "用户名或密码错误"


STATE = MockState()


def _aes_decrypt(value: str):
    """按官方协议解密；解不开返回 None。

    说明：真实服务端对"明文 + t=true"是否也容忍，我们无法用假账号验证
    （账号不存在时在查用户阶段就短路了）。这里按**协议正确语义**实现：
    t=true ⇒ 必须先 AES-128-CBC(key=IV="u2oh6Vu^HWe4_AES") 加密。
    """
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    try:
        from xuexitong.crypto import decrypt
        return decrypt(value)
    except Exception:
        return None


def _decoded_form(form: dict) -> tuple[str, str]:
    if form.get("t") == "true":
        return _aes_decrypt(form.get("uname", "")), _aes_decrypt(form.get("password", ""))
    return form.get("uname", ""), form.get("password", "")


def _parse_form(body: str) -> dict:
    out = {}
    for part in body.split("&"):
        if "=" in part:
            k, _, v = part.partition("=")
            from urllib.parse import unquote_plus
            out[unquote_plus(k)] = unquote_plus(v)
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音
        pass

    # -- helpers --
    def _send(self, code: int, body: str = "", ctype: str = "text/html; charset=utf-8",
              headers: dict | None = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _json(self, obj, code: int = 200, headers: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers)

    def _authed(self) -> bool:
        cookie = self.headers.get("Cookie", "") or ""
        return STATE.login_ok and STATE.token and f"MOCKSESS={STATE.token}" in cookie

    def _redirect_login(self) -> None:
        self._send(302, "", headers={"Location": "/login?fid=0&refer=x"})

    # -- routes --
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/login":
            need = "1" if STATE.need_vcode else ""
            self._send(200, f'<html><input type="hidden" id="needVcode" name="needVcode" value="{need}"/></html>')
            return
        if path == "/base":
            if self._authed():
                self._send(200, "<html>base</html>")
            else:
                self._redirect_login()
            return
        if path == "/apis/login/userLogin4UAP.do":
            if self._authed():
                self._json({"result": "1", "msg": {"uid": "999888", "name": "测试用户"}})
            else:
                self._json({"result": "0", "errorMsg": "服务异常，请稍后重试[50001]"})
            return
        if path == "/mycourse/backclazzdata":
            if not self._authed():
                self._redirect_login()
                return
            self._json({"channelList": [{"content": {"id": "111", "cpi": "2222",
                                                     "course": {"data": [{"id": "333", "name": "测试课程"}]}}}]})
            return
        self._send(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        if path != "/fanyalogin":
            self._send(404, "not found")
            return
        form = _parse_form(body)
        STATE.login_posts.append(form)
        if STATE.need_vcode:
            self._json({"status": False, "msg2": "验证码错误"})
            return
        uname, pwd = _decoded_form(form)
        if uname == USERNAME and pwd == PASSWORD:
            STATE.login_ok = True
            STATE.token = secrets.token_hex(8)
            self._json(
                {"status": True, "uid": "999888", "name": "测试用户", "url": "https://i.chaoxing.com"},
                headers={
                    "Set-Cookie": f"MOCKSESS={STATE.token}; Path=/; Max-Age=3600; HttpOnly",
                },
            )
            return
        self._json({"status": False, "msg2": STATE.fail_msg})


def start() -> tuple[ThreadingHTTPServer, str]:
    """启动 mock，返回 (server, base_url)。"""
    STATE.need_vcode = False
    STATE.login_posts = []
    STATE.login_ok = False
    STATE.token = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()
