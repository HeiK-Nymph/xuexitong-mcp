"""分析结果的落盘（state/analysis/<work_id>.json）。

设计要点：**结果存服务端，不靠调用方在对话里搬运**。
analyze_homework 把模板解析结果 + prompt 存盘并返回 prompt；
generate_homework_docx 只按 work_id 读回来，避免"把整份 JSON 当参数传来传去"。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from .paths import state_dir

_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = state_dir() / "analysis"

__all__ = ["ANALYSIS_DIR", "analysis_path", "save_analysis", "load_analysis"]


def analysis_path(work_id: str, directory: Path | str = ANALYSIS_DIR) -> Path:
    safe = "".join(c for c in str(work_id) if c.isalnum() or c in "-_") or "unknown"
    return Path(directory) / f"{safe}.json"


def save_analysis(payload: dict, work_id: str, directory: Path | str = ANALYSIS_DIR) -> Path:
    p = analysis_path(work_id, directory)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body.setdefault("work_id", str(work_id))
    body["updated_at"] = int(time.time())
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_analysis(work_id: str, directory: Path | str = ANALYSIS_DIR) -> Optional[dict]:
    p = analysis_path(work_id, directory)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def regions_to_json(regions) -> list[dict]:
    return [asdict(r) for r in regions]
