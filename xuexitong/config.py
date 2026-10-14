"""配置读取 —— 凭据全部来自环境变量（MCP env 注入），.env 仅作本地兜底。

优先级：进程环境变量 > 项目根 .env > <作业根>/.xuexitong_mcp/.env
（只用 setdefault，绝不覆盖已存在的环境变量）。
凭据只在本模块被读进内存一次，不落任何 dataclass 之外的磁盘位置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .paths import env_file, state_file

USERNAME = "XUEXITONG_USERNAME"
PASSWORD = "XUEXITONG_PASSWORD"
COOKIE = "XUEXITONG_COOKIE"
STATE_PATH = "XUEXITONG_STATE_PATH"
HOME = "XUEXITONG_HOME"
INSECURE = "XUEXITONG_INSECURE"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class MissingCredential(RuntimeError):
    """既没有可用 cookie，也没有配置账号密码。"""


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 读取（不引入第三方依赖），已存在的环境变量优先。"""
    dotenv_path = Path(path) if path else _PROJECT_ROOT / ".env"
    if not dotenv_path.is_file():
        # 真实凭据放 <作业根>/.xuexitong_mcp/.env；项目目录只留 .env.example
        dotenv_path = env_file()
    if not dotenv_path.is_file():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # 注意用 `key not in os.environ` 而不是 `not os.environ.get(key)`：
        # 显式设为空（例如 --env XUEXITONG_USERNAME=）也是有效意图，不能被 .env 覆盖。
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass(frozen=True)
class AuthConfig:
    """一次运行所需的全部认证输入。"""

    username: str = ""
    password: str = ""
    cookie_blob: str = ""
    state_path: Path = field(default_factory=lambda: state_file("cookies.json"))
    insecure: bool = False

    @property
    def has_password(self) -> bool:
        return bool(self.username and self.password)

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie_blob.strip())

    def describe(self) -> str:
        """用于日志/报错的安全描述（绝不含密文或明文凭据）。"""
        bits = []
        bits.append(f"username={'已配置' if self.username else '未配置'}")
        bits.append("password=" + ("已配置" if self.password else "未配置"))
        bits.append("cookie=" + ("已配置" if self.has_cookie else "未配置"))
        return " ".join(bits)

    @staticmethod
    def from_env(dotenv: bool = True) -> "AuthConfig":
        if dotenv:
            load_dotenv()
        state = os.environ.get(STATE_PATH, "").strip()
        home = os.environ.get(HOME, "").strip()
        if state:
            state_path = Path(state)
        elif home:
            state_path = Path(home) / "cookies.json"
        else:
            state_path = state_file("cookies.json")
        return AuthConfig(
            username=os.environ.get(USERNAME, "").strip(),
            password=os.environ.get(PASSWORD, ""),
            cookie_blob=os.environ.get(COOKIE, "").strip(),
            state_path=state_path,
            insecure=os.environ.get(INSECURE, "").strip() in ("1", "true", "True", "yes"),
        )
