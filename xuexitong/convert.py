"""把 .doc 转成 .docx（学习通原始附件常见 .doc，而 python-docx 只能读 .docx）。

策略链：
  1) Word COM（pywin32）—— 本机装了 Office 时可用，实测可用
  2) LibreOffice soffice（--convert-to docx）
  3) 都没有 → 明确报错，告诉用户手动另存为 .docx

不做"把 .doc 复制成 .docx 再打开"这种假转换（python-docx 会直接报错）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

__all__ = ["ConvertError", "doc_to_docx", "available_converters"]

_SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


class ConvertError(RuntimeError):
    """格式转换失败（含"本机没有转换器"）。"""


def available_converters() -> dict:
    info = {"word_com": False, "soffice": ""}
    try:
        import win32com.client  # noqa: F401

        info["word_com"] = True
    except Exception:  # noqa: BLE001
        pass
    info["soffice"] = (next((p for p in _SOFFICE_CANDIDATES if Path(p).is_file()), "")
                       or (shutil.which("soffice") or ""))
    return info


def doc_to_docx(src: str | Path, dest: Optional[str | Path] = None) -> Path:
    """把 .doc 转成 .docx；已存在且比源文件新的目标会直接复用。"""
    s = Path(src)
    if s.suffix.lower() == ".docx":
        return s
    if s.suffix.lower() != ".doc":
        raise ConvertError(f"只支持 .doc → .docx，收到 {s.suffix}")
    if not s.is_file():
        raise ConvertError(f"文件不存在：{s}")
    out = Path(dest) if dest else s.with_suffix(".docx")
    if out.is_file() and out.stat().st_mtime >= s.stat().st_mtime:
        return out

    info = available_converters()
    if info["word_com"]:
        try:
            return _convert_with_word(s, out)
        except Exception as e:  # noqa: BLE001
            # Word 失败时继续尝试 soffice
            last = f"Word COM 转换失败：{e}"
    else:
        last = "本机没有 pywin32（Word COM 不可用）"

    if info["soffice"]:
        try:
            return _convert_with_soffice(info["soffice"], s, out)
        except Exception as e:  # noqa: BLE001
            last += f"；soffice 也失败：{e}"

    raise ConvertError(
        f"无法把 {s.name} 转成 .docx —— {last}。"
        "请用 Word/WPS 打开该文件后另存为 .docx 放到同目录，我就能继续分析。"
    )


def _convert_with_word(src: Path, dest: Path) -> Path:
    import win32com.client as win32

    word = win32.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        doc = word.Documents.Open(str(src), ReadOnly=True)
        try:
            if dest.exists():
                dest.unlink()
            doc.SaveAs2(str(dest), FileFormat=16)      # 16 = wdFormatXMLDocument
        finally:
            doc.Close(False)
    finally:
        word.Quit()
    if not dest.is_file():
        raise ConvertError("Word 报告成功但目标文件不存在")
    return dest


def _convert_with_soffice(soffice: str, src: Path, dest: Path) -> Path:
    subprocess.run([soffice, "--headless", "--convert-to", "docx", "--outdir",
                    str(dest.parent), str(src)], check=True, capture_output=True, timeout=180)
    if not dest.is_file():
        raise ConvertError("soffice 未产生 .docx")
    return dest
