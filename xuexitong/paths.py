# -*- coding: utf-8 -*-
"""运行时数据目录（state）的解析。

**为什么单独抽出来**：这个项目要能"直接把文件夹发给别人"，所以**任何运行时产生的数据
（登录 cookie、菜单缓存、材料缓存、提交 payload、页面地址表、分析结果）都不能写在项目目录里**；
默认写到用户级目录，跟 gf2_ssr_mcp 一样只留代码 + 文档 + 测试。

优先级：环境变量 `XUEXITONG_STATE_DIR` > 用户级默认
默认：Windows `<作业根>/.xuexitong_mcp`；其它 `~/.local/state/xuexitong_mcp/state`
r"""

from __future__ import annotations

import os
from pathlib import Path

ENV_STATE_DIR = "XUEXITONG_STATE_DIR"
STATE_DIR_NAME = ".xuexitong_mcp"
DEFAULT_DOWNLOAD_ROOT = "D:/学习通作业"


def project_root() -> Path:
    """本项目根目录（server.py / docs 所在那层）。**运行时数据不写这里。**"""
    return Path(__file__).resolve().parent.parent


def download_root() -> Path:
    """作业根目录（附件、完成版文档、state 都在它下面）。可用 XUEXITONG_ROOT 覆盖。"""
    raw = os.environ.get("XUEXITONG_ROOT", "").strip()
    return Path(raw) if raw else Path(DEFAULT_DOWNLOAD_ROOT)


def state_dir() -> Path:
    """state 根目录：默认 `<作业根>/.xuexitong_mcp`（不存在则创建）。

    优先级：`XUEXITONG_STATE_DIR` > `<XUEXITONG_ROOT|D:/学习通作业>/.xuexitong_mcp`
    —— 放在作业根目录里，保证 MCP 项目目录**只有代码**、可直接打包分享。
    """
    raw = os.environ.get(ENV_STATE_DIR, "").strip()
    d = Path(raw) if raw else download_root() / STATE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file(*parts: str) -> Path:
    """state 下的某个文件（父目录自动创建）。"""
    d = state_dir()
    for part in parts[:-1]:
        d = d / part
    d.mkdir(parents=True, exist_ok=True)
    return d / parts[-1]


def env_file() -> Path:
    """凭据文件位置：`<state>/.env`（项目目录里只留 .env.example）。"""
    return state_dir() / ".env"


__all__ = ["ENV_STATE_DIR", "STATE_DIR_NAME", "DEFAULT_DOWNLOAD_ROOT", "project_root",
           "download_root", "state_dir", "state_file", "env_file"]
