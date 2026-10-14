"""把窗口作答的内容写回 DOCX —— **锚点式**，不预设字段。

edits 里每条 = {"anchor": 模板里的原文片段, "content": 内容, "mode": "after"|"replace"|"append"}
  · after   （默认）在该段之后插入新段落
  · replace 用内容替换该段（个人信息段会被拒绝，见下）
  · append  追加到文档末尾

两条硬规则（代码强制，不依赖模型自觉）：
  1. **个人信息不得编造**：目标段落含 姓名/学号/班级/日期… 时，模型给的 content 一律拒绝，
     只接受调用方显式传入的 user_fields；没给就写 ＿＿＿（待填）并汇报给用户。
  2. **信息不足不得编造**：content 写成 {"INSUFFICIENT": true, "reason": "..."} 时，
     写成【待补：原因】并汇报，不编内容。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from .template import personal_fields_in

__all__ = ["EditReport", "apply_edits", "normalize_content", "ASK_PLACEHOLDER"]

ASK_PLACEHOLDER = "＿＿＿（待填）"


@dataclass
class EditReport:
    out_path: str = ""
    file_size: int = 0
    applied: list[dict] = field(default_factory=list)        # {anchor, mode, paragraphs}
    not_found: list[str] = field(default_factory=list)       # 锚点没找到
    refused_personal: list[dict] = field(default_factory=list)  # 拒绝写入的模型内容
    insufficient: list[dict] = field(default_factory=list)
    needs_user: list[dict] = field(default_factory=list)     # 需要用户提供的个人信息
    paragraphs_written: int = 0


def normalize_content(content) -> tuple[str, str]:
    """→ (kind, text)；kind ∈ {TEXT, CODE, INSUFFICIENT}。"""
    if content is None:
        return "TEXT", ""
    if isinstance(content, str):
        return "TEXT", content
    if isinstance(content, dict):
        if content.get("INSUFFICIENT") or str(content.get("type", "")).upper() == "INSUFFICIENT":
            return "INSUFFICIENT", str(content.get("reason") or "缺少必要信息")
        if str(content.get("type", "")).upper() == "CODE":
            return "CODE", str(content.get("content") or content.get("text") or "")
        return "TEXT", str(content.get("content") or content.get("text") or "")
    if isinstance(content, (list, tuple)):
        return "TEXT", "\n".join(normalize_content(c)[1] for c in content)
    return "TEXT", str(content)


# ---------------------------------------------------------------- 样式/写入

def _copy_run_style(run, sample_run) -> None:
    if sample_run is None:
        return
    try:
        if sample_run.font.name:
            run.font.name = sample_run.font.name
            rpr = run._element.get_or_add_rPr()
            rfonts = rpr.find(qn("w:rFonts"))
            if rfonts is None:
                rfonts = rpr.makeelement(qn("w:rFonts"), {})
                rpr.append(rfonts)
            rfonts.set(qn("w:eastAsia"), sample_run.font.name)
        if sample_run.font.size:
            run.font.size = sample_run.font.size
        if sample_run.font.bold is not None:
            run.font.bold = sample_run.font.bold
    except Exception:  # noqa: BLE001
        pass


def _next_paragraph(para: Paragraph) -> Optional[Paragraph]:
    el = para._p.getnext()
    while el is not None and el.tag != qn("w:p"):
        el = el.getnext()
    return Paragraph(el, para._parent) if el is not None else None


def _insert_after(para: Paragraph, text: str, style) -> Paragraph:
    """在 para 之后插入新段落；para 已是最后一段时追加到文档末尾。

    python-docx 只提供 insert_paragraph_before，所以"之后"= 在下一段之前插入。
    注意 append 分支用 para._parent（body 段落的父对象就是 Document），
    不要试图用 XML 元素去构造 Document —— 那样会报 'CT_Document' has no attribute 'seek'。
    """
    nxt = _next_paragraph(para)
    if nxt is not None:
        return nxt.insert_paragraph_before(text, style)
    doc = para._parent
    return doc.add_paragraph(text, style)


def _write_into(para: Paragraph, lines: list[str]) -> int:
    sample = para.runs[0] if para.runs else None
    style = para.style
    para.clear()
    _copy_run_style(para.add_run(lines[0] if lines else ""), sample)
    last = para
    for line in lines[1:]:
        new_para = _insert_after(last, "", style)
        _copy_run_style(new_para.add_run(line), sample)
        last = new_para
    return len(lines)


def _append_lines(doc, lines: list[str]) -> int:
    for line in lines:
        doc.add_paragraph(line)
    return len(lines)


def _find_by_anchor(doc, anchor: str) -> Optional[Paragraph]:
    """锚点定位：先整段相等，再包含匹配。"""
    a = (anchor or "").strip()
    if not a:
        return None
    for para in doc.paragraphs:
        if para.text.strip() == a:
            return para
    for para in doc.paragraphs:
        if a in para.text:
            return para
    return None


# ---------------------------------------------------------------- 主流程

def apply_edits(template_path: str | Path, edits: list[dict],
                *, out_path: Optional[str | Path] = None,
                user_fields: Optional[dict[str, str]] = None) -> EditReport:
    from docx import Document as DocxDocument

    src = Path(template_path)
    doc = DocxDocument(str(src))
    report = EditReport()
    user_fields = user_fields or {}

    # 先处理 replace（就地替换，不影响其它锚点），再处理 after/append
    order = {"replace": 0, "after": 1, "append": 2}
    items = sorted(edits or [], key=lambda e: order.get(str(e.get("mode") or "after").lower(), 1))

    for edit in items:
        anchor = str(edit.get("anchor") or "").strip()
        mode = str(edit.get("mode") or "after").lower()
        kind, text = normalize_content(edit.get("content"))

        if kind == "INSUFFICIENT":
            target = _find_by_anchor(doc, anchor) if anchor else None
            marker = f"【待补：{text}】"
            report.insufficient.append({"anchor": anchor or "(文档末尾)", "reason": text})
            if mode == "append" or target is None:
                report.paragraphs_written += _append_lines(doc, [marker])
            else:
                report.paragraphs_written += _write_into(target, [marker])
            continue

        personal = personal_fields_in(f"{anchor} {text[:50]}")

        if mode == "append":
            if text.strip():
                report.paragraphs_written += _append_lines(doc, text.split("\n"))
                report.applied.append({"anchor": "(append)", "mode": mode,
                                       "paragraphs": len(text.split("\n"))})
            continue

        target = _find_by_anchor(doc, anchor)
        if target is None:
            report.not_found.append(anchor)
            continue

        if personal:
            provided = {f: str(user_fields.get(f, "")).strip() for f in personal}
            missing = [f for f, v in provided.items() if not v]
            if edit.get("content") is not None:
                report.refused_personal.append({"anchor": anchor, "fields": personal})
            line = "    ".join(f"{f}：{provided[f] or ASK_PLACEHOLDER}" for f in personal)
            report.paragraphs_written += _write_into(target, [line])
            report.needs_user.append({"anchor": anchor, "fields": missing})
            continue

        lines = text.replace("\r\n", "\n").split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        if mode == "replace":
            report.paragraphs_written += _write_into(target, lines or [""])
        else:
            last = target
            for line in lines:
                new_para = _insert_after(last, "", target.style)
                _copy_run_style(new_para.add_run(line), target.runs[0] if target.runs else None)
                last = new_para
            report.paragraphs_written += len(lines)
        report.applied.append({"anchor": anchor, "mode": mode, "paragraphs": len(lines)})

    report.applied.sort(key=lambda x: x["anchor"])
    report.not_found.sort()
    report.insufficient.sort(key=lambda x: x["anchor"])

    if out_path is None:
        stamp = time.strftime("%Y%m%d%H%M%S")
        out_path = src.with_name(f"{src.stem}_完成版_{stamp}.docx")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    report.out_path = str(out)
    report.file_size = out.stat().st_size
    return report
