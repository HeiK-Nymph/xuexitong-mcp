"""作业附件：发现 → 下载 → 落盘。

下载链路（与 chaoxing_mcp 一致，实测端点）：
    详情页 HTML 里 <div class="editor-iframe"><iframe objectid=".." filename="..">
    → GET /mooc-ans/ueditorupload/read?objectId=<objectid>
    → 对 .docx/.doc/.pdf 该页只是中转页，需再抓 <a class="btnDown" href=".."> 的真实地址
    → 下载字节流落盘

落盘布局（默认根目录 D:/学习通作业）：
    D:/学习通作业/<作业名>/<附件名>
    D:/学习通作业/<作业名>/_attachments.json   ← 清单（objectId/文件名/大小/来源 URL）
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from .courses import MOOC, FetchError, SessionExpired, _guard
from .homework import Homework
from .paths import DEFAULT_DOWNLOAD_ROOT, download_root

# 默认作业根：**在进程启动时**按 XUEXITONG_ROOT 解析一次（环境变量本来也不会中途变）。
# 以前这里是写死的 DEFAULT_DOWNLOAD_ROOT，导致 XUEXITONG_ROOT 只影响 state 目录、
# 不影响材料落盘 —— 文档说能覆盖，实际覆盖不了（实测踩到）。
DEFAULT_ROOT = str(download_root())
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_BTN_DOWN_1 = re.compile(r'<a[^>]*class="[^"]*btnDown[^"]*"[^>]*href="([^"]+)"', re.I)
_BTN_DOWN_2 = re.compile(r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*btnDown[^"]*"', re.I)

__all__ = [
    "Attachment", "safe_name", "parse_attachments", "parse_prompt_attachments",
    "fetch_detail_html", "download_one", "download_embedded", "fetch_status_download_url",
    "download_homework_attachments", "resolved_root", "DEFAULT_ROOT",
]


@dataclass
class Attachment:
    object_id: str
    filename: str = ""
    url: str = ""
    local_path: str = ""
    downloaded: bool = False
    skipped: bool = False
    size: int = 0
    error: str = ""


def safe_name(name: str, *, max_len: int = 120) -> str:
    """把作业名/附件名变成合法文件名（Windows 不能含 <>:"/\\|?*，且不能以点/空格结尾）。"""
    cleaned = _BAD_CHARS.sub("_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "未命名"
    if len(cleaned) > max_len:
        stem = Path(cleaned)
        cleaned = stem.stem[: max_len - len(stem.suffix) - 1] + stem.suffix
    return cleaned


def resolved_root(root: str | Path, account: str) -> Path:
    """把账号子目录拼到 root 上，且**幂等**。

    两个账号若存在同名作业，直接写同一个目录会互相覆盖/误跳过，
    所以落盘路径统一为 <root>/<账号>/<作业名>/<附件名>。
      · account 为空 → 原样返回（无法确认账号时不擅自改路径）
      · root 末尾已经是该账号目录 → 不重复叠加（方便调用方显式传带账号的 root）
    """
    base = Path(root)
    if not account:
        return base
    if base.name == account:
        return base
    return base / account


def parse_attachments(html: str) -> list[Attachment]:
    """从作业详情 HTML 提取附件（editor-iframe 里的 objectid/filename）。"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Attachment] = []
    seen: set[str] = set()

    candidates = []
    for div in soup.find_all("div", class_="editor-iframe"):
        iframe = div.find("iframe")
        if iframe is not None:
            candidates.append(iframe)
    # 兜底：任何带 objectid 的 iframe
    for iframe in soup.find_all("iframe"):
        if iframe.get("objectid") and iframe not in candidates:
            candidates.append(iframe)

    for iframe in candidates:
        object_id = (iframe.get("objectid") or "").strip()
        if not object_id or object_id in seen:
            continue
        seen.add(object_id)
        filename = (iframe.get("filename") or iframe.get("name") or "").strip()
        out.append(Attachment(
            object_id=object_id,
            filename=filename,
            url=f"{MOOC}/mooc-ans/ueditorupload/read?objectId={object_id}",
        ))
    return out


def parse_prompt_attachments(html: str) -> list[Attachment]:
    """只取**题干**里内嵌的附件（`h3.mark_name` 之内），**不碰作答区**（`.stem_answer`）。

    为什么单独抽一个：很多作业的"题目正文"就是题目里嵌的一份 docx（实测：示例实验·VUE(一)
    的题目就是内嵌的《示例实验·VUE(一).docx》）。这种附件不在"作业附件区"，
    而作答区里同样有 editor-iframe ——  混在一起会把**自己交上去的附件**
    当成题目材料再下一遍。
    """
    soup = BeautifulSoup(html, "html.parser")
    stems = soup.select("h3.mark_name") or soup.select(".mark_name")
    if not stems:                      # 没有题干标记时退化为整个题目容器
        stems = soup.select(".questionLi") or soup.select(".TiMu")
    out: list[Attachment] = []
    seen: set[str] = set()
    for stem in stems:
        for answer_area in stem.select(".stem_answer"):
            answer_area.decompose()
        for iframe in stem.find_all("iframe"):
            object_id = (iframe.get("objectid") or "").strip()
            if not object_id or object_id in seen:
                continue
            seen.add(object_id)
            out.append(Attachment(
                object_id=object_id,
                filename=(iframe.get("filename") or "").strip(),
                url=f"{MOOC}/mooc-ans/ueditorupload/read?objectId={object_id}",
            ))
    return out


def fetch_status_download_url(client: httpx.Client, object_id: str) -> str:
    """题目内嵌附件的直链。站点自己的 insertCloud.html → downloadAttach() 走的就是这条：

        GET /ananas/status/<objectid>
        → {"status": "success", "download": "http://d0.cldisk.com/download/<id>?at_=..&ak_=..&ad_=.."}

    objectid 过期/无权限时返回的 JSON 里没有 download 字段（返回空串）。
    """
    if not object_id:
        return ""
    try:
        resp = client.get(f"{MOOC}/ananas/status/{object_id}",
                          headers={"Referer": f"{MOOC}/"}, timeout=20)
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("download") or "")


def download_embedded(client: httpx.Client, att: Attachment, dest_dir: Path,
                      *, overwrite: bool = False) -> Attachment:
    """下载**题目内嵌**附件：优先 /ananas/status 直链，失败再退回 read 中转页。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not att.url:      # 兜底：调用方只给了 objectid 时自己补上 read 中转页地址
        att.url = f"{MOOC}/mooc-ans/ueditorupload/read?objectId={att.object_id}"
    url = fetch_status_download_url(client, att.object_id)
    if url:
        try:
            resp = client.get(url, headers={"Referer": f"{MOOC}/"}, timeout=60,
                              follow_redirects=True)
            if resp.status_code == 200 and resp.content:
                content = resp.content
                att.filename = att.filename or f"{att.object_id}.bin"
                target = dest_dir / safe_name(att.filename)
                if target.exists() and not overwrite and target.stat().st_size == len(content):
                    att.skipped = True
                else:
                    target.write_bytes(content)
                    att.downloaded = True
                att.local_path = str(target)
                att.size = len(content)
                return att
            att.error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as e:
            att.error = f"{type(e).__name__}: {e}"
        except OSError as e:
            att.error = f"写入失败: {e}"
    return download_one(client, att, dest_dir, overwrite=overwrite)


def get_standard_enc(client: httpx.Client, hw: Homework) -> str:
    """standardEnc：部分详情页需要它才能正常打开（参考实现的做法）。"""
    try:
        resp = client.get(f"{MOOC}/mooc-ans/work/isExpire",
                          params={"classId": hw.class_id, "workRelationId": hw.work_id,
                                  "cpi": hw.cpi, "courseId": hw.course_id}, timeout=15)
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    try:
        data = resp.json()
    except ValueError:
        return ""
    if isinstance(data, dict) and data.get("status") == 0:
        return str(data.get("standardEnc") or "")
    return ""


def fetch_detail_html(client: httpx.Client, hw: Homework, *, use_standard_enc: bool = True) -> tuple[str, str]:
    """抓作业详情页。返回 (html, 最终 URL)。"""
    url = hw.detail_url or hw.url
    if not url:
        raise FetchError(f"作业《{hw.name}》没有详情 URL")
    if use_standard_enc and "doHomeWorkNew" in url and "standardEnc" not in url:
        enc = get_standard_enc(client, hw)
        if enc:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}standardEnc={enc}"
    resp = client.get(url, timeout=25)
    _guard(resp, f"获取作业详情({hw.name})")
    return resp.text, str(resp.url)


def _filename_from_disposition(resp: httpx.Response) -> str:
    cd = resp.headers.get("content-disposition", "") or ""
    m = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", cd, re.I)
    if not m:
        return ""
    from urllib.parse import unquote

    return unquote(m.group(1).strip().strip('"'))


def resolve_real_url(client: httpx.Client, att: Attachment) -> str:
    """docx/doc/pdf 的 read 页只是中转页，需要取 btnDown 的真实地址。"""
    ext = Path(att.filename).suffix.lower()
    if ext not in (".docx", ".doc", ".pdf"):
        return att.url
    resp = client.get(att.url, headers={"Referer": f"{MOOC}/mooc-ans/ueditorupload/read"}, timeout=20)
    if resp.status_code != 200:
        return att.url
    html = resp.text
    m = _BTN_DOWN_1.search(html) or _BTN_DOWN_2.search(html)
    if not m:
        return att.url
    href = m.group(1).strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return MOOC + href
    return att.url


def download_one(client: httpx.Client, att: Attachment, dest_dir: Path, *, overwrite: bool = False) -> Attachment:
    """下载单个附件到 dest_dir。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        url = resolve_real_url(client, att)
        resp = client.get(url, headers={"Referer": f"{MOOC}/mooc-ans/ueditorupload/read"},
                          timeout=60, follow_redirects=True)
        if resp.status_code != 200:
            att.error = f"HTTP {resp.status_code}"
            return att
        content = resp.content
        if not content:
            att.error = "响应为空"
            return att

        filename = att.filename or _filename_from_disposition(resp) or f"{att.object_id}.bin"
        att.filename = filename
        target = dest_dir / safe_name(filename)
        if target.exists() and not overwrite and target.stat().st_size == len(content):
            att.skipped = True
            att.local_path = str(target)
            att.size = len(content)
            return att
        target.write_bytes(content)
        att.local_path = str(target)
        att.size = len(content)
        att.downloaded = True
    except httpx.HTTPError as e:
        att.error = f"{type(e).__name__}: {e}"
    except OSError as e:
        att.error = f"写入失败: {e}"
    return att


def download_homework_attachments(client: httpx.Client, hw: Homework, *,
                                  root: str | Path = DEFAULT_ROOT,
                                  overwrite: bool = False,
                                  dump_dir: Optional[str | Path] = None) -> dict:
    """下载一份作业的全部附件。

    返回 {homework, folder, detail_url, total, downloaded, skipped, failed, attachments[]}
    """
    folder = Path(root) / safe_name(hw.name)
    result: dict = {
        "homework": hw.name, "status": hw.status_raw, "course_id": hw.course_id,
        "work_id": hw.work_id, "folder": str(folder), "detail_url": hw.detail_url or hw.url,
        "total": 0, "downloaded": 0, "skipped": 0, "failed": 0, "attachments": [],
    }
    try:
        html, final_url = fetch_detail_html(client, hw)
    except (FetchError, SessionExpired) as e:
        result["error"] = str(e)
        return result
    result["detail_url"] = final_url

    attachments = parse_attachments(html)
    result["total"] = len(attachments)
    if not attachments:
        if dump_dir:
            p = Path(dump_dir)
            p.mkdir(parents=True, exist_ok=True)
            (p / f"{safe_name(hw.name)}.html").write_text(html, encoding="utf-8")
            result["html_dump"] = str(p / f"{safe_name(hw.name)}.html")
        result["note"] = "详情页里没有 editor-iframe 附件"
        return result

    for att in attachments:
        att = download_one(client, att, folder, overwrite=overwrite)
        if att.downloaded:
            result["downloaded"] += 1
        elif att.skipped:
            result["skipped"] += 1
        else:
            result["failed"] += 1
        result["attachments"].append(asdict(att))

    manifest = folder / "_attachments.json"
    manifest.write_text(json.dumps({
        "homework": hw.name, "work_id": hw.work_id, "course_id": hw.course_id,
        "status": hw.status_raw, "detail_url": final_url,
        "saved_at": int(time.time()),
        "attachments": [a for a in result["attachments"]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    result["manifest"] = str(manifest)
    return result
