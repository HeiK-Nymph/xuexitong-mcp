"""xuexitong_mcp 的认证层。

注意：包内 `login` / `session` / `crypto` / `config` 都是子模块名，
所以对外导出的登录函数叫 login_with_password，避免 `xuexitong.login` 被函数遮蔽。
"""

from .config import AuthConfig, MissingCredential
from .crypto import TRANSFER_KEY, decrypt, encrypt
from .login import (
    LoginOutcome, LoginResult, SessionInfo,
    ensure_session, is_login_location, login_with_password, probe, probe_business,
    redirected_to_login,
)
from .session import (
    Cookie, apply_cookies, build_client, cookies_from_client, load_cookies,
    parse_cookie_blob, save_cookies,
)

__all__ = [
    "AuthConfig", "MissingCredential",
    "TRANSFER_KEY", "encrypt", "decrypt",
    "LoginOutcome", "LoginResult", "SessionInfo",
    "ensure_session", "is_login_location", "login_with_password", "probe",
    "probe_business", "redirected_to_login",
    "Cookie", "apply_cookies", "build_client", "cookies_from_client",
    "load_cookies", "parse_cookie_blob", "save_cookies",
]
