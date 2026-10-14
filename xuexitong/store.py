"""落盘缓存：课程菜单 URL 必须按账号隔离。

菜单 URL 里带 enc / openc（会话级令牌）。换账号后若沿用旧缓存，
轻则请求报错，重则拿到上一个账号的数据 —— 所以缓存文件带 account 字段，
账号不匹配时整份作废并重建。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .paths import state_dir

_ROOT = Path(__file__).resolve().parent.parent
MENU_CACHE = state_dir() / "menu_cache.json"

__all__ = ["MENU_CACHE", "current_identity", "account_dir_name",
           "load_menu_cache", "save_menu_cache", "menu_key"]


def _log(message: str) -> None:
    print(f"[xuexitong.store] {message}", file=sys.stderr, flush=True)


def current_identity(client=None) -> str:
    """当前身份标识：优先用配置的账号，其次用 cookie 里的 _uid。"""
    from .config import AuthConfig

    cfg = AuthConfig.from_env()
    if cfg.username.strip():
        return cfg.username.strip()
    if client is not None:
        from .login import uid_from_cookies

        return uid_from_cookies(client)
    return ""


def account_dir_name(client=None) -> str:
    """落盘用的账号目录名：**优先 uid**（稳定且不含手机号等隐私），退化到配置的账号名。"""
    from .attachments import safe_name

    if client is not None:
        from .login import uid_from_cookies

        uid = uid_from_cookies(client)
        if uid:
            return safe_name(uid)
    from .config import AuthConfig

    raw = AuthConfig.from_env().username.strip()
    # 账号未知时必须返回空串：safe_name("") 会回落到"未命名"，那样会造出
    # D:/学习通作业/未命名/ 这种误导性目录。空串让 resolved_root 保持原样。
    return safe_name(raw) if raw else ""


def menu_key(course_id: str, class_id: str) -> str:
    return f"{course_id}:{class_id}"


def load_menu_cache(account: str, path: Path | str = MENU_CACHE) -> dict:
    """读菜单缓存；账号不匹配（或无账号标记的旧格式）一律返回空。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    if "entries" not in raw:                      # 旧格式（扁平 dict，无账号信息）
        _log("菜单缓存是旧格式（无账号标记），已作废重建")
        return {}
    if str(raw.get("account", "")) != str(account):
        _log(f"菜单缓存属于账号 {raw.get('account') or '未知'}，当前 {account}，已作废重建")
        return {}
    entries = raw.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_menu_cache(entries: dict, account: str, path: Path | str = MENU_CACHE) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"account": account, "saved_at": int(time.time()), "entries": entries}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p
