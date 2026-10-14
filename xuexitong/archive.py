"""压缩包处理：解压 / 列目录。

真实场景：学习通作业的**原始附件常常是压缩包**（rar/zip/7z），模板与素材都在里面；
而作业文件夹里同时可能躺着"我交上去的填写版"。所以分析前必须先解开压缩包。

解压器优先级（本机实测）：
    .zip            → 标准库 zipfile
    .tar/.gz/.tgz   → 标准库 tarfile
    .rar            → UnRAR.exe / WinRAR.exe / 7z / bsdtar（按可用性）
    .7z             → 7z
找不到解压器时**明确报错**，绝不假装"包里没东西"。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}

_UNRAR_CANDIDATES = [
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
]
_WINRAR_CANDIDATES = [
    r"C:\Program Files\WinRAR\WinRAR.exe",
    r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
]

__all__ = ["ARCHIVE_SUFFIXES", "ArchiveError", "ExtractResult", "is_archive",
           "extract_archive", "list_archive", "available_tools"]


class ArchiveError(RuntimeError):
    """压缩包处理失败（含"本机没有解压器"这种可预期失败）。"""


@dataclass
class ExtractResult:
    ok: bool = False
    tool: str = ""
    dest: str = ""
    files: List[str] = field(default_factory=list)
    total_size: int = 0
    error: str = ""


def is_archive(path: str | Path) -> bool:
    p = Path(path)
    return p.suffix.lower() in ARCHIVE_SUFFIXES


def _which(*names: str) -> str:
    for n in names:
        hit = shutil.which(n)
        if hit:
            return hit
    return ""


def available_tools() -> dict:
    """本机可用的解压能力（便于把"为什么解不开"讲清楚）。"""
    return {
        "zip": "zipfile(内置)",
        "tar": "tarfile(内置)",
        "unrar": next((p for p in _UNRAR_CANDIDATES if Path(p).is_file()), "") or _which("unrar"),
        "winrar": next((p for p in _WINRAR_CANDIDATES if Path(p).is_file()), ""),
        "7z": _which("7z", "7za", "7zr"),
        "bsdtar": _which("bsdtar"),
    }


def _run(cmd: list[str], *, timeout: int = 180) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, creationflags=0x08000000)  # CREATE_NO_WINDOW
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError as e:
        return 127, f"可执行文件不存在: {e}"
    except subprocess.TimeoutExpired:
        return 124, "解压超时"


def extract_archive(path: str | Path, dest: str | Path) -> ExtractResult:
    """解压到 dest；返回解出来的文件列表。失败时 error 里写清原因与可用工具。"""
    src = Path(path)
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()
    res = ExtractResult(dest=str(out))

    if not src.is_file():
        res.error = f"压缩包不存在：{src}"
        return res

    if suffix == ".zip":
        try:
            with zipfile.ZipFile(src) as z:
                bad = z.testzip()
                if bad:
                    res.error = f"zip 校验失败于 {bad}"
                    return res
                z.extractall(out)
            res.tool = "zipfile"
        except zipfile.BadZipFile as e:
            res.error = f"不是有效的 zip：{e}"
            return res
    elif suffix in (".tar", ".gz", ".tgz", ".bz2", ".xz"):
        try:
            with tarfile.open(src) as t:
                t.extractall(out, filter="data") if sys.version_info >= (3, 12) else t.extractall(out)
            res.tool = "tarfile"
        except tarfile.TarError as e:
            res.error = f"tar 解压失败：{e}"
            return res
    elif suffix in (".rar", ".7z"):
        tools = available_tools()
        attempts: list[tuple[str, list[str]]] = []
        if suffix == ".rar":
            if tools["unrar"]:
                attempts.append((f"unrar({Path(tools['unrar']).name})",
                                 [tools["unrar"], "x", "-y", "-o+", str(src), str(out) + "\\"]))
            if tools["winrar"]:
                attempts.append(("winrar", [tools["winrar"], "x", "-y", "-ibck", str(src), str(out) + "\\"]))
        if tools["7z"]:
            attempts.append(("7z", [tools["7z"], "x", "-y", f"-o{out}", str(src)]))
        if tools["bsdtar"]:
            attempts.append(("bsdtar", [tools["bsdtar"], "-xf", str(src), "-C", str(out)]))
        if not attempts:
            res.error = (f"本机没有可解压 {suffix} 的工具（试过 unrar/WinRAR/7z/bsdtar）。"
                         "请安装 WinRAR 或 7-Zip，或手动解压后把文件放进同一目录。")
            return res
        last = ""
        for name, cmd in attempts:
            code, output = _run(cmd)
            if code == 0:
                res.tool = name
                break
            last = f"{name} 退出码 {code}: {output.strip()[:200]}"
        else:
            res.error = f"{suffix} 解压失败 —— {last}"
            return res
    else:
        res.error = f"不支持的压缩格式：{suffix}"
        return res

    files = sorted(p for p in out.rglob("*") if p.is_file())
    res.files = [str(p) for p in files]
    res.total_size = sum(p.stat().st_size for p in files)
    res.ok = True
    return res


def list_archive(path: str | Path) -> list[str]:
    """只列目录内容（不落盘），用于报告"压缩包里有什么"。"""
    src = Path(path)
    suffix = src.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            return z.namelist()
    if suffix in (".tar", ".gz", ".tgz", ".bz2", ".xz"):
        with tarfile.open(src) as t:
            return t.getnames()
    tools = available_tools()
    if suffix == ".rar":
        exe = tools["unrar"] or tools["winrar"]
        if exe:
            code, output = _run([exe, "l", str(src)])
            return [ln for ln in output.splitlines() if ln.strip()][:200]
    if tools["7z"]:
        code, output = _run([tools["7z"], "l", str(src)])
        return [ln for ln in output.splitlines() if ln.strip()][:200]
    raise ArchiveError(f"没有可用于列出 {suffix} 内容的工具")
