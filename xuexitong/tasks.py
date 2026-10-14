"""对外任务层：MCP 工具直接调这里，返回 JSON 可序列化的 dict（都带 summary）。

设计要点：
  · 会话在进程内复用（cookie 保留在内存），失效时自动重建一次
  · 抓取失败一律抛 TaskError / 记入 errors，不静默返回空
  · 只读任务不带任何凭据参数：凭据只来自环境变量 / .env 兜底（项目根 .env → <作业根>/.xuexitong_mcp/.env）
"""

from __future__ import annotations

import json
import re
import random
import threading
import time
from html import unescape as _html_unescape
from pathlib import Path
from typing import Iterable, Optional

from . import archive, convert, submit, worklist
from .attachments import DEFAULT_ROOT, download_homework_attachments, resolved_root
from .config import AuthConfig
from .courses import (
    AntispiderError, FetchError, SessionExpired,
    find_homework_menu, get_course_list, get_course_menu,
)
from .homework import Homework, fetch_homework_list, status_distribution
from . import store
from .login import ensure_session
from .paths import state_dir, state_file

_ROOT = Path(__file__).resolve().parent.parent
MENU_CACHE = state_dir() / "menu_cache.json"
DEFAULT_SLEEP = 0.55


class TaskError(RuntimeError):
    """任务层可预期的失败（会作为 tool 错误文本返回给 agent）。"""


# ---------------------------------------------------------------- 会话

_lock = threading.RLock()
_client = None
_session_summary = ""


def get_client(*, force: bool = False):
    """进程内复用的 httpx 客户端；失效时重建。"""
    global _client, _session_summary
    with _lock:
        if _client is None or force:
            if _client is not None:
                try:
                    _client.close()
                except Exception:  # noqa: BLE001
                    pass
                _client = None
            cfg = AuthConfig.from_env()
            info = ensure_session(cfg)
            _session_summary = info.summary()
            if not info.ok:
                raise TaskError(f"会话不可用：{info.error_code} - {info.message}")
            _client = info.client
        return _client


def session_summary() -> str:
    return _session_summary


def _with_relogin(fn, *args, **kwargs):
    """跑一次；遇到会话失效就重建会话再跑一次。"""
    try:
        return fn(get_client(), *args, **kwargs)
    except SessionExpired:
        return fn(get_client(force=True), *args, **kwargs)


# ---------------------------------------------------------------- 菜单缓存

def _identity(client=None) -> str:
    return store.current_identity(client)


class _MenuStub:
    __slots__ = ("effective_url", "data", "href", "name")

    def __init__(self, url: str):
        self.effective_url = url
        self.data = url
        self.href = url
        self.name = "作业"


def _homework_menu_url(client, course, cache: dict) -> str:
    key = f"{course.course_id}:{course.class_id}"
    cached = cache.get(key, {}).get("url")
    if cached:
        return cached
    items, _ = get_course_menu(client, course)
    menu = find_homework_menu(items)
    if menu is None:
        raise FetchError(f"课程《{course.course_name}》菜单里没有'作业'入口")
    url = menu.effective_url or menu.data or menu.href
    cache[key] = {"url": url, "course_name": course.course_name}
    return url


# ---------------------------------------------------------------- 只读任务

def whoami() -> dict:
    """当前会话状态（不发起业务请求）。"""
    cfg = AuthConfig.from_env()
    try:
        client = get_client()
    except TaskError as e:
        return {
            "ok": False, "error": str(e),
            "credentials": cfg.describe(), "state_path": str(cfg.state_path),
            "summary": f"会话不可用：{e}（凭据状态 {cfg.describe()}）",
        }
    valid, uid, name, detail = _with_relogin(_probe)
    return {
        "ok": bool(valid), "uid": uid, "name": name, "detail": detail,
        "credentials": cfg.describe(), "state_path": str(cfg.state_path),
        "session": session_summary(),
        "summary": (f"会话可用：uid={uid or '未知'} name={name or '未知'}；"
                    f"凭据 {cfg.describe()}；cookie 文件 {cfg.state_path}"),
    }


def _probe(client):
    from .login import probe

    return probe(client)


def list_courses() -> dict:
    courses = _with_relogin(get_course_list)
    items = [{"course_id": c.course_id, "course_name": c.course_name,
              "class_id": c.class_id, "cpi": c.cpi, "url": c.url} for c in courses]
    return {
        "count": len(items), "courses": items,
        "summary": f"共 {len(items)} 门课程",
    }


def collect_homeworks(*, course_ids: Optional[Iterable[str]] = None, max_courses: int = 0,
                      sleep: float = DEFAULT_SLEEP, use_cache: bool = True) -> dict:
    """扫描课程并汇总作业（内部函数）。"""
    client = get_client()
    try:
        courses = get_course_list(client)
    except SessionExpired:
        courses = get_course_list(get_client(force=True))

    wanted = {str(x) for x in course_ids or [] if str(x)}
    if wanted:
        courses = [c for c in courses if c.course_id in wanted]
    if max_courses:
        courses = courses[:max_courses]

    cache = store.load_menu_cache(_identity(client)) if use_cache else {}
    homeworks: list[Homework] = []
    errors: list[str] = []
    for course in courses:
        try:
            url = _homework_menu_url(client, course, cache)
            hws, _ = fetch_homework_list(client, course, _MenuStub(url))
            homeworks.extend(hws)
        except SessionExpired:
            try:
                client = get_client(force=True)
                url = _homework_menu_url(client, course, cache)
                hws, _ = fetch_homework_list(client, course, _MenuStub(url))
                homeworks.extend(hws)
            except (FetchError, AntispiderError) as e:
                errors.append(f"{course.course_name}: {e}")
        except AntispiderError as e:
            errors.append(f"{course.course_name}: 反爬未通过（{e}）")
            break
        except FetchError as e:
            errors.append(f"{course.course_name}: {e}")
        if sleep:
            time.sleep(sleep)
    if use_cache and cache:
        store.save_menu_cache(cache, _identity(client))
    return {"courses": courses, "homeworks": homeworks, "errors": errors}


def list_homeworks(*, course_id: Optional[str] = None, status: str = "any",
                   max_courses: int = 0, sleep: float = DEFAULT_SLEEP) -> dict:
    """列作业。status: any | unfinished | completed；返回原始状态文案供核查。"""
    data = collect_homeworks(course_ids=[course_id] if course_id else None,
                            max_courses=max_courses, sleep=sleep)
    hws: list[Homework] = data["homeworks"]
    if status == "unfinished":
        picked = [h for h in hws if h.is_unfinished_strict]
    elif status == "completed":
        picked = [h for h in hws if h.status_num == 2]
    else:
        picked = hws

    items = [{
        "course_id": h.course_id, "course_name": _course_name(data["courses"], h.course_id),
        "work_id": h.work_id, "name": h.name, "status": h.status_raw,
        "status_num": h.status_num, "is_unfinished": h.is_unfinished_strict,
        "start_time": h.start_time, "end_time": h.end_time,
        "detail_url": h.detail_url or h.url,
    } for h in picked]

    dist = status_distribution(hws)
    unknown = [h.name for h in hws if h.status_num == 3]
    return {
        "count": len(items), "status_filter": status,
        "scanned_courses": len(data["courses"]),
        "all_status_distribution": dist,
        "unrecognized_status_items": unknown[:20],
        "homeworks": items, "errors": data["errors"],
        "summary": (f"扫描 {len(data['courses'])} 门课、作业 {len(hws)} 条，"
                    f"命中 status={status} 的 {len(items)} 条；全部状态文案分布 {dist}"
                    + (f"；未识别状态 {len(unknown)} 条（原文见 unrecognized_status_items）" if unknown else "")
                    + (f"；异常 {len(data['errors'])} 项" if data["errors"] else "")),
    }


def _course_name(courses, course_id: str) -> str:
    for c in courses:
        if c.course_id == course_id:
            return c.course_name
    return ""


# ---------------------------------------------------------------- 下载任务

def _download_many(client, targets: list[Homework], root: str, *, overwrite: bool = False) -> dict:
    files = 0
    size = 0
    failed = 0
    details = []
    for hw in targets:
        res = download_homework_attachments(client, hw, root=root, overwrite=overwrite)
        files += res["downloaded"]
        failed += res["failed"]
        size += sum(a["size"] for a in res["attachments"] if a["downloaded"])
        details.append({
            "homework": hw.name, "status": hw.status_raw, "course_id": hw.course_id,
            "work_id": hw.work_id, "folder": res["folder"],
            "total": res["total"], "downloaded": res["downloaded"],
            "skipped": res["skipped"], "failed": res["failed"],
            "note": res.get("note", ""), "error": res.get("error", ""),
            "attachments": [{"filename": a["filename"] or a["object_id"],
                             "size": a["size"], "downloaded": a["downloaded"],
                             "skipped": a["skipped"], "error": a["error"]}
                            for a in res["attachments"]],
        })
    return {"homework_count": len(targets), "file_count": files, "bytes": size,
            "failed_files": failed, "details": details}


def download_unfinished(*, root: str = DEFAULT_ROOT, dry_run: bool = False,
                        course_ids: Optional[Iterable[str]] = None,
                        max_courses: int = 0, sleep: float = DEFAULT_SLEEP,
                        overwrite: bool = False) -> dict:
    """【正式功能】下载全部未完成（待做）作业的附件。"""
    data = collect_homeworks(course_ids=course_ids, max_courses=max_courses, sleep=sleep)
    hws: list[Homework] = data["homeworks"]
    targets = [h for h in hws if h.is_unfinished_strict]

    client = get_client()
    account = store.account_dir_name(client)
    eff_root = str(resolved_root(root, account))
    if dry_run:
        return {
            "dry_run": True, "unfinished_count": len(targets), "root": eff_root, "account_dir": account,
            "scanned_courses": len(data["courses"]), "scanned_homeworks": len(hws),
            "targets": [{"course_name": _course_name(data["courses"], h.course_id),
                         "name": h.name, "status": h.status_raw,
                         "folder": f"{eff_root}/{h.name}"} for h in targets],
            "errors": data["errors"],
            "summary": (f"[预演] 扫描 {len(data['courses'])} 门课 / {len(hws)} 条作业，"
                        f"其中未完成(待做) {len(targets)} 条；不会下载任何文件"),
        }
    if not targets:
        return {
            "unfinished_count": 0, "scanned_courses": len(data["courses"]),
            "scanned_homeworks": len(hws), "all_status_distribution": status_distribution(hws),
            "errors": data["errors"],
            "summary": (f"未找到未完成(待做)作业：扫描 {len(data['courses'])} 门课、{len(hws)} 条作业；"
                        f"状态文案分布 {status_distribution(hws)}；未下载任何文件"),
        }

    result = _download_many(client, targets, eff_root, overwrite=overwrite)
    result.update({
        "unfinished_count": len(targets), "root": eff_root, "account_dir": account,
        "scanned_courses": len(data["courses"]), "scanned_homeworks": len(hws),
        "errors": data["errors"],
    })
    err_note = f"；扫描异常 {len(data['errors'])} 项" if data["errors"] else ""
    result["summary"] = (
        f"未完成(待做)作业 {len(targets)} 条，附件共下载 {result['file_count']} 个文件 / "
        f"{result['bytes']/1024:.1f} KB，失败 {result['failed_files']} 个；"
        f"落盘目录 {eff_root}（按 账号/作业名 分级）{err_note}"
    )
    return result


def download_specific(*, work_id: str, course_id: str = "", root: str = DEFAULT_ROOT,
                      overwrite: bool = False) -> dict:
    """下载指定作业的附件（按 work_id；course_id 可选用于缩小扫描范围）。"""
    if not work_id:
        raise TaskError("work_id 不能为空")
    data = collect_homeworks(course_ids=[course_id] if course_id else None)
    match = next((h for h in data["homeworks"] if h.work_id == str(work_id)), None)
    if match is None:
        raise TaskError(f"没找到 work_id={work_id} 的作业"
                        f"（扫描 {len(data['courses'])} 门课 / {len(data['homeworks'])} 条作业）")
    client = get_client()
    account = store.account_dir_name(client)
    eff_root = str(resolved_root(root, account))
    res = download_homework_attachments(client, match, root=eff_root, overwrite=overwrite)
    if res.get("error"):
        raise TaskError(res["error"])
    res["root"] = eff_root
    res["account_dir"] = account
    res["summary"] = (
        f"《{match.name}》(status={match.status_raw}) 附件 {res['total']} 个 → "
        f"下载 {res['downloaded']}、跳过 {res['skipped']}、失败 {res['failed']}；目录 {res['folder']}"
        + (f"；{res['note']}" if res.get("note") else "")
    )
    return res


def download_random_completed(*, n: int = 3, root: str = DEFAULT_ROOT,
                             overwrite: bool = False) -> dict:
    """测试用：随机挑 N 个已完成作业下载附件（不是 MCP 的正式功能）。"""
    data = collect_homeworks()
    done = [h for h in data["homeworks"] if h.status_num == 2]
    if not done:
        raise TaskError("没有已完成作业可供测试")
    n = max(1, min(int(n), len(done)))
    targets = random.sample(done, n)
    client = get_client()
    account = store.account_dir_name(client)
    eff_root = str(resolved_root(root, account))
    result = _download_many(client, targets, eff_root, overwrite=overwrite)
    result.update({"mode": "completed-random", "done_total": len(done),
                   "root": eff_root, "account_dir": account,
                   "errors": data["errors"],
                   "picked": [h.name for h in targets]})
    result["summary"] = (
        f"[测试模式] 从 {len(done)} 条已完成作业中随机取 {len(targets)} 条，"
        f"附件下载 {result['file_count']} 个文件 / {result['bytes']/1024:.1f} KB，"
        f"失败 {result['failed_files']} 个；落盘目录 {eff_root}"
    )
    return result


# ---------------------------------------------------------------- 材料准备（查询 / 下载 / 解压 / 读取）

MATERIAL_KINDS = {".jpg": "图像", ".jpeg": "图像", ".png": "图像", ".bmp": "图像", ".gif": "图像",
                  ".pdf": "PDF", ".txt": "文本", ".md": "文本", ".csv": "表格", ".xlsx": "表格",
                  ".xls": "表格", ".ppt": "幻灯片", ".pptx": "幻灯片",
                  ".py": "代码", ".c": "代码", ".cpp": "代码", ".java": "代码", ".m": "代码", ".sql": "代码",
                  ".ipynb": "代码", ".docx": "文档", ".doc": "文档",
                  ".rar": "压缩包", ".zip": "压缩包", ".7z": "压缩包"}
TEMPLATE_HINTS_IN_NAME = ("填写", "模板", "报告", "实验")
SUSPICIOUS_NAME_HINTS = ("完成版", "已填", "答案", "参考答案", "我的")


def _homework_folder(root: str, account: str, homework_name: str) -> Path:
    from .attachments import safe_name

    return Path(resolved_root(root, account)) / safe_name(homework_name)


def _kind_of(suffix: str) -> str:
    return MATERIAL_KINDS.get(suffix.lower(), "其它")


# 取材料时要跳过的"噪音目录"：依赖、缓存、IDE 配置。
# 实测踩过：学生把实验工程放在作业目录里（demo-app/node_modules 等），
# 一次扫描吞进 797 个文件、还把几十个 node_modules 的 README.md 当"可读材料"
# 交给模型 —— 上下文直接爆掉。这类目录一律不进清单。
NOISE_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".idea", ".vscode", ".next", ".nuxt", ".cache",
})
MAX_INVENTORY_FILES = 400


def _inventory(folder: Path, *, include_extracted: bool = False,
               warnings: list[str] | None = None) -> list[dict]:
    if not folder.is_dir():
        return []
    out: list[dict] = []
    skipped: set[str] = set()
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.name == "_attachments.json":
            continue
        rel = f.relative_to(folder)
        if not include_extracted and rel.parts and rel.parts[0] == "_extracted":
            continue
        noise = [p for p in rel.parts[:-1] if p in NOISE_DIRS]
        if noise:
            skipped.update(noise)
            continue
        out.append({"path": str(f), "rel": str(rel), "suffix": f.suffix.lower(),
                    "size": f.stat().st_size, "kind": _kind_of(f.suffix), "source": "直接"})
        if len(out) >= MAX_INVENTORY_FILES:
            if warnings is not None:
                warnings.append(f"目录里文件过多，只列了前 {MAX_INVENTORY_FILES} 个（其余已忽略）")
            break
    if skipped and warnings is not None:
        warnings.append("已跳过依赖/缓存目录（不算材料）：" + "、".join(sorted(skipped)))
    return out


def _extract_archives(folder: Path) -> tuple[list[dict], list[str], list[str]]:
    mats: list[dict] = []
    notes: list[str] = []
    warnings: list[str] = []
    for item in _inventory(folder):
        if item["suffix"] not in (".rar", ".zip", ".7z", ".tar", ".gz", ".tgz"):
            continue
        src = Path(item["path"])
        dest = folder / "_extracted" / src.stem
        res = archive.extract_archive(src, dest)
        if not res.ok:
            warnings.append(f"压缩包 {item['rel']} 解压失败：{res.error}")
            continue
        notes.append(f"{item['rel']} → 解压出 {len(res.files)} 个文件（{res.tool}）")
        for f in res.files:
            p = Path(f)
            mats.append({"path": str(p), "rel": p.name, "suffix": p.suffix.lower(),
                         "size": p.stat().st_size, "kind": _kind_of(p.suffix),
                         "source": f"压缩包: {item['rel']}"})
    return mats, notes, warnings


def _text_of(path: Path, max_chars: int) -> tuple[list[str], str, bool]:
    """返回 (段落列表, 错误, 是否截断)。"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        from .template import read_document

        doc = read_document(path)
        if doc.error:
            return [], doc.error, False
        paras = doc.paragraphs
    elif suffix in (".txt", ".md", ".csv"):
        try:
            paras = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return [], f"读取失败：{e}", False
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return [], "缺少 pypdf，无法读取 PDF", False
        try:
            paras = []
            for pg in PdfReader(str(path)).pages[:20]:
                paras.extend((pg.extract_text() or "").splitlines())
        except Exception as e:  # noqa: BLE001
            return [], f"PDF 解析失败：{e}", False
    else:
        return [], f"不支持的文本读取类型（{suffix or '无后缀'}）", False

    total = sum(len(x) for x in paras)
    if total > max_chars:
        acc, cut = [], 0
        for line in paras:
            if cut + len(line) > max_chars:
                break
            acc.append(line)
            cut += len(line)
        return acc, "", True
    return paras, "", False


def _resolve_homework(work_id: str, course_id: str = "") -> tuple[Homework, str]:
    data = collect_homeworks(course_ids=[course_id] if course_id else None)
    match = next((h for h in data["homeworks"] if h.work_id == str(work_id)), None)
    if match is None:
        raise TaskError(f"没找到 work_id={work_id} 的作业"
                        f"（扫描 {len(data['courses'])} 门课 / {len(data['homeworks'])} 条作业）")
    return match, _course_name(data["courses"], match.course_id)


def probe_homework_files(*, work_id: str, course_id: str = "", root: str = DEFAULT_ROOT) -> dict:
    """只读诊断：这个作业到底有哪些附件、压缩包里有什么、本机能不能解压/转换。"""
    client = get_client()
    account = store.account_dir_name(client)
    match, course_name = _resolve_homework(work_id, course_id)
    folder = _homework_folder(root, account, match.name)
    inv = _inventory(folder)
    archives = []
    for item in inv:
        if item["suffix"] in (".rar", ".zip", ".7z", ".tar", ".gz", ".tgz"):
            try:
                entries = [e for e in archive.list_archive(item["path"]) if e.strip()][:50]
            except Exception as e:  # noqa: BLE001
                entries = [f"<读取失败: {e}>"]
            archives.append({"file": item["rel"], "entries": entries})
    return {
        "work_id": work_id, "homework_name": match.name, "course_name": course_name,
        "folder": str(folder), "files": inv, "archives": archives,
        "extract_tools": archive.available_tools(), "doc_converters": convert.available_converters(),
        "summary": (f"《{match.name}》目录共 {len(inv)} 个文件"
                    + (f"，含压缩包 {len(archives)} 个" if archives else "")),
    }


def _material_meta(work_id: str) -> dict:
    """从 materials 缓存里取作业名/目录（**离线**、不联网）。

    给 prepare_homework_submission / list_prepared_submissions 用：后者是离线工具，
    拿不到作业名和目录就会输出"名称未知 / 未找到完成版文档"（实测误导过一次）。
    """
    import json as _json

    p = state_file("materials", f"{work_id}.json")
    if not p.is_file():
        return {}
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not data.get("folder"):
        return {}
    return {"homework_name": str(data.get("homework_name") or ""),
            "course_name": str(data.get("course_name") or ""),
            "folder": str(data.get("folder") or "")}


def _fetch_prompt_attachments(work_id: str, course_id: str,
                              folder: Path) -> tuple[list[dict], list[str]]:
    """把**题目里内嵌的附件**取回作业目录（题目正文就是一份文档的作业）。

    为什么必须有：这类作业的"材料"不在作业附件区，目录扫描只会看到空目录 →
    直接 NEED_MORE_INFO，整条"拉取并做作业"就断在这里（实测：示例实验·VUE(一)
    的题目就是题目里内嵌的《示例实验·VUE(一).docx》，最后靠手工调
    /ananas/status/<objectid> 才解开）。

    全程 **best-effort**：任何异常都只记一条 note，绝不让取材料本身失败。
    """
    from .attachments import download_embedded, parse_prompt_attachments

    notes: list[str] = []
    try:
        page, _ = _resolve_submit_page(work_id=work_id, course_id=course_id)
        html = get_client().get(page.url, timeout=25).text or ""
        atts = parse_prompt_attachments(html)
        if not atts:
            return [], notes
        client = get_client()
        out: list[dict] = []
        for att in atts:
            got = download_embedded(client, att, folder)
            out.append({"filename": got.filename, "object_id": got.object_id, "size": got.size,
                        "path": got.local_path, "downloaded": got.downloaded,
                        "skipped": got.skipped, "error": got.error})
            if got.error:
                notes.append(f"题目内嵌附件《{got.filename or got.object_id}》没取到：{got.error}")
        ok = [a for a in out if not a["error"]]
        if ok:
            notes.append("已从**题目内嵌附件**取回 %d 个文件：%s"
                         % (len(ok), "、".join(a["filename"] for a in ok)))
        return out, notes
    except Exception as e:            # noqa: BLE001 —— 取材料不该被这个附加动作拖垮
        notes.append(f"取题目内嵌附件失败（不影响其它材料）：{type(e).__name__}: {e}")
        return [], notes


def get_homework_materials(*, work_id: str, course_id: str = "", root: str = DEFAULT_ROOT,
                           refresh: bool = False, max_chars_per_doc: int = 30000) -> dict:
    """【取材料】把该作业**所有相关文件**（含压缩包内）的清单与全文交给当前窗口。

    本工具只做：查目录 → （目录里没有可读文档时）**把"题目内嵌附件"取回本地** →
    解压 → .doc 转 .docx → 读取文本。
    **不预设待填字段、不做分析、不调用任何 LLM** —— 分析由当前窗口自己完成。
    """
    import json as _json

    if not work_id:
        raise TaskError("work_id 不能为空")
    work_id = str(work_id)
    cache_path = state_file("materials", f"{work_id}.json")

    if not refresh and cache_path.is_file():
        try:
            cached = _json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if cached and Path(cached.get("folder", "")).is_dir():
            pass  # 仍要重新读文本（文件可能变了），但目录信息可复用
    client = get_client()
    account = store.account_dir_name(client)
    match, course_name = _resolve_homework(work_id, course_id)
    folder = _homework_folder(root, account, match.name)

    warnings: list[str] = []
    extract_notes: list[str] = []
    files = _inventory(folder, warnings=warnings)

    # 题目正文常常就是"题目里内嵌的一份文档"：目录里没有可读文档时，先去作答页把它取回来。
    prompt_attachments: list[dict] = []
    if not any(f["suffix"] in (".docx", ".doc", ".pdf") for f in files):
        prompt_attachments, prompt_notes = _fetch_prompt_attachments(work_id, course_id, folder)
        warnings.extend(prompt_notes)
        files = _inventory(folder, warnings=warnings)

    if not files:
        return _need_more(work_id, match.name, course_name, folder, [], [],
                          ["该作业**没有任何附件**（含压缩包；也没在题目里发现内嵌附件），"
                           "不能据此完成任务。",
                           "如果你手上有原始资料/模板，请放进该目录后重试。"], warnings)

    extracted, extract_notes, arch_warn = _extract_archives(folder)
    warnings.extend(arch_warn)
    all_files = files + extracted

    # .doc → .docx（让窗口能读到内容）
    for item in list(all_files):
        if item["suffix"] != ".doc":
            continue
        try:
            new_path = convert.doc_to_docx(item["path"])
        except convert.ConvertError as e:
            warnings.append(str(e))
            continue
        all_files.append({"path": str(new_path), "rel": f"{item['rel']} → {new_path.name}",
                          "suffix": ".docx", "size": new_path.stat().st_size,
                          "kind": "文档", "source": item["source"] + "（已转换）"})

    documents: list[dict] = []
    for item in all_files:
        if item["suffix"] not in (".docx", ".txt", ".md", ".csv", ".pdf"):
            continue
        paras, err, truncated = _text_of(Path(item["path"]), max_chars_per_doc)
        if err:
            warnings.append(f"{item['rel']}：{err}")
            continue
        hints = []
        if item["suffix"] == ".docx":
            from .template import read_document

            hints = [{"paragraph_index": h.paragraph_index, "kind": h.kind,
                      "after": h.after_text[:80], "personal_fields": h.personal_fields}
                     for h in read_document(Path(item["path"])).hints]
        documents.append({
            "file": item["rel"], "path": item["path"], "source": item["source"],
            "paragraph_count": len(paras), "truncated": truncated,
            "text": "\n".join(f"{i:>4}| {t}" for i, t in enumerate(paras)),
            "blank_hints": hints,
            "personal_fields": sorted({f for h in hints for f in h.get("personal_fields", [])}),
        })

    if not documents:
        kinds = sorted({f["kind"] for f in all_files})
        return _need_more(work_id, match.name, course_name, folder, all_files, extract_notes,
                          [f"附件里**没有可读文档**（只有 {('、'.join(kinds)) or '未知类型'}）："
                           f"{'、'.join(f['rel'] for f in all_files[:8])}",
                           "本工具只能把文档内容交给窗口阅读；若任务需要原始模板/资料，请补充。"],
                          warnings)

    # 建议的模板（只是建议：可读文档里"空白提示"最多的那个）
    docx_docs = [d for d in documents if d["file"].lower().endswith((".docx", ".doc"))]
    suggestion = None
    if docx_docs:
        best = max(docx_docs, key=lambda d: (len(d["blank_hints"]),
                                             any(k in d["file"] for k in TEMPLATE_HINTS_IN_NAME),
                                             -any(k in d["file"] for k in SUSPICIOUS_NAME_HINTS)))
        reasons = [f"空白提示 {len(best['blank_hints'])} 处"]
        if any(k in best["file"] for k in SUSPICIOUS_NAME_HINTS):
            reasons.append("文件名像已完成稿，谨慎使用")
        suggestion = {"file": best["file"], "path": best["path"], "reasons": reasons}
        if len(best["blank_hints"]) == 0:
            warnings.append(f"候选文档《{best['file']}》没有发现空白位置，可能已经填过；"
                            "请自行判断它是原始模板还是成品。")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(_json.dumps({
        "work_id": work_id, "homework_name": match.name, "course_name": course_name,
        "folder": str(folder), "files": all_files, "extract_notes": extract_notes,
        "template_suggestion": suggestion, "warnings": warnings,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    personal = sorted({f for d in documents for f in d.get("personal_fields", [])})
    return {
        "status": "OK",
        "work_id": work_id, "homework_name": match.name, "course_name": course_name,
        "folder": str(folder),
        "files": [{"rel": f["rel"], "kind": f["kind"], "size": f["size"], "source": f["source"]}
                  for f in all_files],
        "extract_notes": extract_notes,
        "documents": documents,
        "template_suggestion": suggestion,
        "personal_fields": personal,
        "prompt_attachments": prompt_attachments,
        "warnings": warnings,
        "materials_path": str(cache_path),
        "summary": (f"《{match.name}》材料已就绪：文件 {len(all_files)} 个"
                    f"（{'；'.join(extract_notes) if extract_notes else '无压缩包'}），"
                    f"可读文档 {len(documents)} 份"
                    + (f"；建议模板：{suggestion['file']}" if suggestion else "")
                    + (f"；题目内嵌附件已取回 {len(prompt_attachments)} 个"
                       if prompt_attachments else "")
                    + (f"；个人信息字段（{ '、'.join(personal) }）不得由模型编造，需用 user_fields 提供"
                       if personal else "")
                    + (f"；注意：{'；'.join(warnings)}" if warnings else "")),
    }


def _need_more(work_id: str, homework_name: str, course_name: str, folder: Path,
               inventory: list[dict], extract_notes: list[str], reasons: list[str],
               warnings: list[str] | None = None) -> dict:
    return {
        "status": "NEED_MORE_INFO",
        "work_id": work_id, "homework_name": homework_name, "course_name": course_name,
        "folder": str(folder), "files": inventory, "extract_notes": extract_notes,
        "reasons": reasons, "warnings": warnings or [],
        "summary": (f"⚠️ 无法继续《{homework_name}》：{reasons[0]} "
                    f"（目录：{folder}，共 {len(inventory)} 个文件）"
                    " —— 请把所需材料补进该目录后重试；在材料齐备前我不会编造任何内容。"),
    }


def apply_homework_edits(*, work_id: str, edits: list[dict], template: str = "",
                         user_fields: dict | None = None, root: str = DEFAULT_ROOT,
                         output_dir: str = "", overwrite: bool = False) -> dict:
    """【写回】按锚点把内容写进模板，生成完成版 DOCX。

    edits: [{"anchor": 模板里的原文片段, "content": "...", "mode": "after"|"replace"|"append"}]
      · anchor 用 get_homework_materials 返回的文档原文里的片段（整段相等优先，其次包含）
      · 个人信息段（姓名/学号/班级/日期…）：模型给的值一律拒绝，只接受 user_fields
      · content 写成 {"INSUFFICIENT": true, "reason": "..."} → 文档里标【待补：原因】
    """
    import json as _json

    from .docx_fill import apply_edits

    if not work_id:
        raise TaskError("work_id 不能为空")
    work_id = str(work_id)
    if isinstance(edits, str):
        try:
            edits = _json.loads(edits)
        except ValueError as e:
            raise TaskError(f"edits 不是合法 JSON：{e}") from None
    if not isinstance(edits, list) or not edits:
        raise TaskError("edits 为空：请先用 get_homework_materials 读取材料，"
                        "再用 anchor+content 指定要写什么、写在哪")
    if isinstance(user_fields, str):
        try:
            user_fields = _json.loads(user_fields)
        except ValueError as e:
            raise TaskError(f"user_fields 不是合法 JSON：{e}") from None

    folder: Path | None = None
    tpl_path: Path | None = None
    cache_path = state_file("materials", f"{work_id}.json")
    meta: dict = {}
    if cache_path.is_file():
        try:
            meta = _json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    if template:
        cand = Path(template)
        tpl_path = cand if cand.is_absolute() else (Path(meta.get("folder", ".")) / template)
    else:
        sug = (meta.get("template_suggestion") or {}).get("path", "")
        tpl_path = Path(sug) if sug else None
    if tpl_path is None or not tpl_path.is_file():
        raise TaskError("找不到要写入的模板：请先调用 get_homework_materials（或用 template 显式指定）")

    # 输出默认落到"作业目录"，而不是模板所在的 _extracted 缓存目录
    folder = Path(meta.get("folder") or tpl_path.parent)
    out_dir = Path(output_dir) if output_dir else folder
    out_path = out_dir / f"{tpl_path.stem}_完成版.docx"
    if out_path.exists() and not overwrite:
        import time as _t

        out_path = out_dir / f"{tpl_path.stem}_完成版_{_t.strftime('%Y%m%d%H%M%S')}.docx"

    report = apply_edits(tpl_path, edits, out_path=out_path, user_fields=user_fields or {})
    need = [f"{n['anchor'][:24]}（{'、'.join(n['fields'])}）" for n in report.needs_user if n["fields"]]
    return {
        "work_id": work_id, "template": str(tpl_path), "output": report.out_path,
        "file_size": report.file_size,
        "applied_count": len(report.applied), "applied": report.applied,
        "not_found": report.not_found,
        "refused_personal": report.refused_personal,
        "insufficient": report.insufficient, "needs_user": report.needs_user,
        "paragraphs_written": report.paragraphs_written,
        "summary": (f"已生成 {report.out_path}（{report.file_size/1024:.1f} KB；"
                    f"写入 {len(report.applied)} 处、{report.paragraphs_written} 段）"
                    + (f"；⚠️ 需你手工填写：{'；'.join(need)}" if need else "")
                    + (f"；⚠️ 已标【待补】{len(report.insufficient)} 处："
                       f"{'；'.join(i['anchor'][:20] + ' - ' + i['reason'][:40] for i in report.insufficient)}"
                       if report.insufficient else "")
                    + (f"；已拒绝模型编造的个人信息：{[r['anchor'][:20] for r in report.refused_personal]}"
                       if report.refused_personal else "")
                    + (f"；锚点没找到 {len(report.not_found)} 处：{report.not_found}" if report.not_found else "")),
    }


# ---------------------------------------------------------------- 提交链路（只准备，不擅自提交）

SUBMIT_CACHE_DIR = state_dir() / "submit"
STU_PAGES = worklist.StuPageCache(state_file("stu_pages.json"))
PAGES_JSON = SUBMIT_CACHE_DIR / "pages.json"


def _pages_json() -> dict:
    if not PAGES_JSON.is_file():
        return {}
    try:
        data = json.loads(PAGES_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_page(work_id: str, *, page_url: str, answer_id: str, standard_enc: str, enc: str) -> None:
    """把自动发现/收割到的作答页地址写进 state/submit/pages.json（供子 agent 与后续复用）。"""
    data = _pages_json()
    data[str(work_id)] = {"page_url": page_url, "answer_id": answer_id,
                          "standard_enc": standard_enc, "enc": enc, "harvested_at": int(time.time())}
    PAGES_JSON.parent.mkdir(parents=True, exist_ok=True)
    PAGES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _menu_params(match) -> dict:
    """拿这门课的菜单票据 enc/openc/cpi（优先读菜单缓存，通常零额外请求）。"""
    client = get_client()
    account = store.current_identity(client)
    cache = store.load_menu_cache(account)
    key = store.menu_key(match.course_id, match.class_id)
    url = (cache.get(key) or {}).get("url", "")
    if not url:
        found = None
        for c in get_course_list(client):
            if c.course_id == str(match.course_id) and c.class_id == str(match.class_id):
                found = c
                break
        if found is None:
            return {}
        url = _homework_menu_url(client, found, cache)
        store.save_menu_cache(cache, account)
    return worklist.parse_menu_params(url)


def _auto_discover_enc(match) -> tuple[str, str]:
    """纯 HTTP 自动拿作业的 enc。零用户输入：课程菜单 enc/openc → 体验新版页 → 作业列表。"""
    try:
        params = _menu_params(match)
    except Exception as exc:  # noqa: BLE001
        params, menu_err = {}, f"读课程菜单失败：{exc}"
    else:
        menu_err = ""
    account = store.account_dir_name(get_client())
    stu_url = STU_PAGES.get(course_id=match.course_id, class_id=match.class_id, account=account)
    if not params.get("enc") and not stu_url:
        return "", ((menu_err + "；" if menu_err else "")
                    + (STU_PAGES.last_note + "；" if STU_PAGES.last_note else "")
                    + "课程菜单里没拿到 enc/openc，也没注册过新版课程页 URL")
    try:
        found = worklist.discover_course_encs(get_client(), course_id=match.course_id,
                                              class_id=match.class_id,
                                              cpi=match.cpi or params.get("cpi", ""),
                                              enc=params.get("enc", ""),
                                              openc=params.get("openc", ""), stu_url=stu_url)
    except Exception as exc:  # noqa: BLE001
        return "", f"自动发现 enc 失败：{exc}"
    item = (found.get("works") or {}).get(str(match.work_id))
    if not item:
        return "", ("新版作业列表里没有这个作业（" + "；".join(found.get("notes") or [])
                    + f"；列表共 {len(found.get('works') or {})} 条）")
    kind = worklist.detect_work_kind(get_client(), item["task_url"])
    if kind == "view":
        return "", ("该作业已截止：站点对新版作答页只给查看页(/mooc-ans/mooc2/work/view)，"
                    "没有可作答/可上传的页面，无法走上传与提交。")
    return item["enc"], ""


REVIEW_TOKEN_NOTE = (
    "提交闸门：**先展示、后提交**。prepare_homework_submission 会给出 review_token，"
    "你必须把成品清单摆给用户、用户审核说『提交』之后，再把同一个 review_token 传给 submit_homework。"
)


def _review_token_for(url: str, fields: dict) -> str:
    """payload 内容的稳定指纹：内容不变则 token 不变，内容变了 token 必变。

    这不是"用户已审核"的证明（MCP 无法验证人是否真的看过），而是一道**协议闸门**：
    没有走过 prepare（=没有拿到该展示给用户的成品清单），就凑不出这个 token。
    """
    import hashlib

    blob = json.dumps({"url": url, "fields": fields}, ensure_ascii=False, sort_keys=True)
    return "rv-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _submit_cache_path(work_id: str) -> Path:
    SUBMIT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return SUBMIT_CACHE_DIR / f"{work_id}.json"


def _load_submit_cache(work_id: str) -> dict:
    import json as _json

    p = _submit_cache_path(work_id)
    if not p.is_file():
        return {}
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_submit_cache(work_id: str, data: dict) -> None:
    import json as _json

    _submit_cache_path(work_id).write_text(_json.dumps(data, ensure_ascii=False, indent=2),
                                           encoding="utf-8")


def _resolve_submit_page(*, work_id: str, course_id: str = "", page_url: str = "",
                         enc: str = "") -> tuple[submit.AnswerPage, dict]:
    """定位并打开新版作答页（每次重新 GET，保证 uploadTimeStamp 是新鲜的）。"""
    cache = _load_submit_cache(work_id)
    url = page_url or cache.get("page_url", "")
    std = cache.get("standard_enc", "")
    if not url:
        match, _course_name = _resolve_homework(work_id, course_id)
        client = get_client()
        std = submit.get_standard_enc(client, course_id=match.course_id, class_id=match.class_id,
                                      cpi=match.cpi, work_id=match.work_id) or std
        auto_note = ""
        if not enc:
            enc, auto_note = _auto_discover_enc(match)      # 纯 HTTP 自动发现，不用浏览器
        if not enc:
            raise TaskError(
                "缺少作答页地址，自动发现也没成功。"
                + (f"原因：{auto_note} " if auto_note else "")
                + "兜底办法（按顺序）：① 重新调一次（菜单票据偶尔过期）；"
                "② 若这门课菜单里没有「体验新版」入口，用 register_study_page 给一次新版课程页 URL；"
                "③ 最后才用浏览器打开该作业作答页、把地址栏 URL 通过 page_url 传入。"
            )
        url = submit.build_answer_page_url(course_id=match.course_id, class_id=match.class_id,
                                           cpi=match.cpi, work_id=match.work_id,
                                           answer_id=match.work_answer_id, standard_enc=std, enc=enc)
        _remember_page(work_id, page_url=url, answer_id=str(match.work_answer_id or ""),
                       standard_enc=std, enc=enc)
    page = submit.open_answer_page(get_client(), course_id=cache.get("course_id", ""),
                                   class_id=cache.get("class_id", ""), cpi=cache.get("cpi", ""),
                                   work_id=work_id, answer_id=cache.get("answer_id", ""),
                                   standard_enc=std, page_url=url)
    _save_submit_cache(work_id, {
        "work_id": work_id, "page_url": page.url or url, "standard_enc": page.standard_enc or std,
        "course_id": page.course_id, "class_id": page.class_id, "cpi": page.cpi,
        "answer_id": page.answer_id, "homework_name": cache.get("homework_name", ""),
        "folder": cache.get("folder", ""),
    })
    return page, cache


def register_study_page(*, course_id: str = "", stu_url: str, class_id: str = "",
                         cpi: str = "", probe: bool = True) -> dict:
    """【兜底，通常用不到】注册「新版学生课程页」URL → 之后作业的 enc 全自动发现。

    正常情况下 `enc` 是**全自动**的（课程菜单 enc/openc → 体验新版页 → 作业列表），本工具只在
    菜单里拿不到 `openc`（个别课程没有"体验新版"入口）时才需要人工给一次。
    票据按**账号**缓存（账号一变自动作废，需重新给一次）。

    stu_url 形如：
      https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=..&clazzid=..&cpi=..&enc=..&t=..
    （浏览器里从「新版课程页 → 作业」那一步复制地址栏即可）
    """
    info = worklist.parse_stu_url(stu_url)
    if not info["is_stu_page"]:
        raise TaskError(
            "这不像新版课程页 URL：应为 https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu"
            "?courseid=…&clazzid=…&cpi=…&enc=…&t=…（含 mycourse/stu 和 enc/t 参数）")
    cid = str(course_id or info["course_id"])
    clz = str(class_id or info["class_id"])
    cp = str(cpi or info["cpi"])
    if not (cid and clz):
        raise TaskError("URL 里缺 courseid/clazzid，请显式传 course_id / class_id")
    account = store.account_dir_name(get_client())
    STU_PAGES.put(course_id=cid, class_id=clz, stu_url=stu_url, account=account)

    found = worklist.discover_course_encs(get_client(), course_id=cid, class_id=clz, cpi=cp,
                                          stu_url=stu_url)
    rows: list[dict] = []
    for wid, item in (found.get("works") or {}).items():
        kind, note = "unknown", ""
        if probe:
            try:
                resp = get_client().get(item["task_url"], timeout=25)
                kind = worklist.classify_task_url(str(resp.url))
                if kind == "other":
                    kind = worklist.classify_task_url(resp.headers.get("location", ""))
            except Exception as exc:  # noqa: BLE001
                kind, note = "error", str(exc)[:120]
        row = {"work_id": wid, "answer_id": item["answer_id"], "enc": item["enc"], "kind": kind,
               "task_url": item["task_url"]}
        if kind == "dowork":
            std = ""
            try:
                std = submit.get_standard_enc(get_client(), course_id=cid, class_id=clz, cpi=cp,
                                              work_id=wid)
                page_url = submit.build_answer_page_url(course_id=cid, class_id=clz, cpi=cp,
                                                        work_id=wid, answer_id=item["answer_id"],
                                                        standard_enc=std, enc=item["enc"])
                _remember_page(wid, page_url=page_url, answer_id=item["answer_id"],
                               standard_enc=std, enc=item["enc"])
                row["page_url"] = page_url
            except Exception as exc:  # noqa: BLE001
                note = f"记 pages.json 失败：{exc}"[:120]
        if note:
            row["note"] = note
        rows.append(row)
    usable = [r for r in rows if r["kind"] == "dowork"]
    closed = [r for r in rows if r["kind"] == "view"]
    return {
        "course_id": cid, "class_id": clz, "account": account, "work_enc": found.get("work_enc", ""),
        "works": rows, "notes": found.get("notes") or [],
        "summary": (f"已注册新版课程页（课程 {cid}）：workEnc={'有' if found.get('work_enc') else '无'}，"
                    f"作业 {len(rows)} 个，可作答 {len(usable)} 个（已写入 pages.json），"
                    f"已截止只有查看页 {len(closed)} 个"
                    + ("；" + "；".join(found.get("notes") or []) if found.get("notes") else "")),
    }


def open_homework_submit_page(*, work_id: str, course_id: str = "", page_url: str = "",
                              enc: str = "") -> dict:
    """只读：打开新版作答页并回显令牌（uploadEnc/uploadTimeStamp/userId、题目 id、表单 action）。"""
    page, _ = _resolve_submit_page(work_id=work_id, course_id=course_id, page_url=page_url, enc=enc)
    return {
        "work_id": work_id, "page_url": page.url, "html_len": page.html_len,
        "course_id": page.course_id, "class_id": page.class_id, "cpi": page.cpi,
        "answer_id": page.answer_id, "standard_enc": page.standard_enc,
        "question_ids": page.question_ids,
        "add_times": page.add_times, "submit_limit": page.submit_limit,
        "has_form_action": bool(page.form_action),
        "form_fields": sorted(page.form_fields)[:20],
        "summary": (f"作答页已就绪：题目 {len(page.question_ids)} 个 {page.question_ids}，"
                    f"上传令牌={'齐全' if page.tokens_ok else '缺失'}，"
                    f"提交次数 {page.add_times}/{page.submit_limit}，"
                    f"表单 action {'已取到' if page.form_action else '未取到'}"),
    }


def upload_homework_attachment(*, work_id: str, file: str, course_id: str = "", page_url: str = "",
                               enc: str = "", dry_run: bool = False,
                               root: str = DEFAULT_ROOT) -> dict:
    """把本地文件传到作业附件接口（**只上传**：不挂载、不保存、不提交）。

    dry_run=True 时只回显将要发送的请求，不真的上传。
    """
    path = Path(file)
    if not path.is_file():
        raise TaskError(f"文件不存在：{file}")
    data = path.read_bytes()
    page, _ = _resolve_submit_page(work_id=work_id, course_id=course_id, page_url=page_url, enc=enc)
    if dry_run:
        return {
            "dry_run": True, "file": str(path), "size": len(data),
            "request": {"method": "POST", "url": submit.UPLOAD_URL,
                        "params": {"uploadtype": "work", "source": "1", "enc2": page.upload_enc,
                                   "t": page.upload_timestamp, "uid": page.user_id},
                        "multipart_field": "upfile", "filename": path.name},
            "summary": f"[预演] 将把 {path.name}（{len(data)} B）POST 到附件接口；未真正上传。",
        }
    f = submit.upload_attachment(get_client(), page, filename=path.name, data=data)
    return {
        "work_id": work_id, "file": str(path), "objectid": f.objectid, "filename": f.filename,
        "filetype": f.filetype, "size": f.size, "url": f.url, "object_id_enc": f.object_id_enc,
        "insert_html": f.insert_html,
        "summary": (f"附件已上传：{f.filename}（{f.size} B，objectid={f.objectid}）。"
                    f"注意：只是上传，**未挂到作答、未保存、未提交**。"
                    f"insert_html 可在准备提交时拼进答案正文。"),
    }


def prepare_homework_submission(*, work_id: str, files: list[str] | None = None,
                                answers: dict | None = None, answer_qid: str = "",
                                save_only: bool = False, course_id: str = "", page_url: str = "",
                                enc: str = "", root: str = DEFAULT_ROOT) -> dict:
    """【只准备，不提交】把附件拼进答案正文，产出将要提交的 payload 供审阅。

    · files：本地文件路径，会**真实上传**（只上传）并把 insert_html 追加到答案正文
    · answers：{题目id: HTML} 直接覆盖对应答案
    · save_only=True → pyFlag=1（保存），否则 pyFlag=3（提交）
    · root 只是与工具 schema 对齐的兼容参数（本函数用绝对路径/state 目录，不依赖它）
    · **本函数从不 POST 提交表单**，只返回 url/fields 供你确认后再决定
    """
    page, _ = _resolve_submit_page(work_id=work_id, course_id=course_id, page_url=page_url, enc=enc)
    qids = page.question_ids
    target = answer_qid or (qids[0] if len(qids) == 1 else "")
    if (files or answers) and not target:
        raise TaskError(f"该作业有多个题目 {qids}，请用 answer_qid 指定要把内容写进哪一题")

    merged: dict[str, str] = dict(answers or {})
    uploaded: list[dict] = []
    if files:
        base = merged.get(target) or page.form_fields.get(f"answer{target}", "")
        for fp in files:
            p = Path(fp)
            if not p.is_file():
                raise TaskError(f"文件不存在：{fp}")
            f = submit.upload_attachment(get_client(), page, filename=p.name, data=p.read_bytes())
            uploaded.append({"objectid": f.objectid, "filename": f.filename, "size": f.size,
                             "filetype": f.filetype, "insert_html": f.insert_html})
            base = (base + "\n" + f.insert_html) if base else f.insert_html
        merged[target] = base

    payload = submit.prepare_payload(page, answers=merged or None,
                                     py_flag=submit.PYFLAG_SAVE if save_only else submit.PYFLAG_SUBMIT)
    fields = payload["fields"]
    review_token = _review_token_for(payload["url"], fields)
    answer_preview = {k: (v[:80] + "…" if len(v) > 80 else v) for k, v in fields.items()
                      if k.startswith("answer")}
    mode = "保存(pyFlag=1)" if save_only else "提交(pyFlag=3)"
    # 按契约落盘：payload 必须可在磁盘上被主 agent 审阅（子 agent 彩排时指出过这个缺口）
    import json as _json

    payload_path = SUBMIT_CACHE_DIR / f"{work_id}.payload.json"
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    # 把作业名/目录补进缓存：list_prepared_submissions 是**离线**的，拿不到就会输出
    # "名称未知 / 未找到完成版文档"（实测误导过一次）。优先用 materials 缓存，不额外联网。
    meta = _material_meta(work_id)
    _cached = _load_submit_cache(work_id)
    if meta:
        _cached = {**_cached,
                   "homework_name": _cached.get("homework_name") or meta["homework_name"],
                   "folder": meta["folder"]}
        _save_submit_cache(work_id, _cached)
    payload_path.write_text(_json.dumps(
        {"work_id": work_id, "saved_at": int(time.time()),
         "homework_name": _cached.get("homework_name", ""),
         "folder": _cached.get("folder", ""),
         "review_token": review_token, "reviewed_by_user": False,
         "post_url": payload["url"], "method": payload["method"], "fields": fields,
         "mode": mode, "answer_keys": payload["answer_keys"], "note": payload["note"]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "payload_path": str(payload_path),
        "work_id": work_id, "mode": mode, "page_url": page.url,
        "post_url": payload["url"], "method": payload["method"],
        "field_count": len(fields), "answer_keys": payload["answer_keys"],
        "answer_preview": answer_preview,
        "uploaded": [{k: v for k, v in u.items() if k != "insert_html"} for u in uploaded],
        "fields_full": fields,
        "note": payload["note"],
        "review_token": review_token,
        "review_checklist": [
            "把下面这些**原文**摆给用户看，等他审核：",
            f"· 将提交的作业：{work_id}",
            f"· 附件（会随作业一起交）：{[u['filename'] for u in uploaded] or '无'}",
            f"· 提交 payload：{payload_path}（{len(fields)} 字段，{mode}）",
            "· 需要用户自己填的个人信息（姓名/学号/班级…）——**没提供就不要提交**",
            "· 材料不足、我没有编造的地方",
            f"用户审核说『提交』后，再调用 submit_homework(work_id=\"{work_id}\", confirm=true, "
            f"review_token=\"{review_token}\")。",
        ],
        "summary": (f"[未提交] 已组装{mode} payload：POST {payload['url'][:80]}…，"
                    f"{len(fields)} 个字段，答案字段 {payload['answer_keys']}"
                    + (f"，本次上传附件 {len(uploaded)} 个" if uploaded else "")
                    + "。本工具不会发这个请求；要不要真提交由你决定。"),
    }


def submit_homework(*, work_id: str, confirm: bool = False, files: list[str] | None = None,
                    answers: dict | None = None, answer_qid: str = "", course_id: str = "",
                    page_url: str = "", enc: str = "", review_token: str = "",
                    root: str = DEFAULT_ROOT) -> dict:
    """【真正提交，不可逆，**只允许主 agent 调用**】把答案（可含附件）提交到学习通。

    **两道硬门禁**（缺一不可，否则零请求）：
      1. `confirm=True`；
      2. `review_token` == 当前 payload 的指纹 —— 它只由 prepare_homework_submission 产出。
         这道闸门的意义：**完成和提交永远不能一步做完**。你必须先把成品摆给用户、
         用户审核说『提交』，才能拿到并回传这个 token。

    对应手动路径里"点提交 → 确认提交"的最后一下。
    """
    # —— 闸门一之半：confirm=true 时没有 review_token 就**在发任何请求之前**直接拒绝
    if confirm and not review_token:
        raise TaskError(
            "拒绝提交：缺少 review_token。本服务的纪律是**先展示、后提交** —— "
            "禁止把「做完作业」和「提交作业」一步做完。请先 prepare_homework_submission，"
            "把 review_checklist 里的成品清单原文摆给用户；用户审核说『提交』之后，"
            "再带上那个 review_token 重新调用（零请求，本次没有发出任何提交请求）。"
        )

    prepared = prepare_homework_submission(
        work_id=work_id, files=files, answers=answers, answer_qid=answer_qid,
        save_only=False, course_id=course_id, page_url=page_url, enc=enc)

    if not confirm:
        return {
            "submitted": False, "confirm_required": True,
            "post_url": prepared["post_url"], "field_count": prepared["field_count"],
            "answer_keys": prepared["answer_keys"], "uploaded": prepared["uploaded"],
            "fields_full": prepared["fields_full"],
            "review_token": prepared["review_token"],
            "review_checklist": prepared["review_checklist"],
            "summary": ("未提交：需要显式 confirm=true 才会真的提交（不可逆）。"
                        f"将要 POST 的是 {prepared['post_url'][:80]}…，"
                        f"{prepared['field_count']} 个字段，答案字段 {prepared['answer_keys']}"
                        + (f"，本次已上传附件 {len(prepared['uploaded'])} 个" if prepared["uploaded"] else "")
                        + "。"),
        }

    # —— 闸门二：必须持有 prepare 阶段产出的 review_token（=走过"展示给用户"这一步）
    if review_token != prepared["review_token"]:
        raise TaskError(
            "拒绝提交：review_token 与当前 payload 不匹配（成品/附件/payload 变过了）。"
            "请重新 prepare_homework_submission 并把**新的**成品清单摆给用户审核，再提交。"
        )

    page, _cache = _resolve_submit_page(work_id=work_id, course_id=course_id,
                                        page_url=page_url, enc=enc)
    guard = submit.check_submit_guard(page)
    if guard:
        raise TaskError(guard)
    # prepared["fields_full"] 是「表单字段字典」，不是 {题目id: 答案HTML}！当成 answers 传会
    # 让 prepare_payload 把每个字段名都当题目 id，写出 answercourseId 之类的垃圾键，
    # 而真正的 answer<qid> 不被附件正文覆盖 → 提交上去是空答案（站点只回 success，不报错）。
    # 用 extra_fields 直接覆盖字段才是对的。
    payload = submit.prepare_payload(page, extra_fields=prepared["fields_full"] or None,
                                     py_flag=submit.PYFLAG_SUBMIT)
    data = submit.post_submission(get_client(), payload, referer=page.url)
    return {
        "submitted": True, "work_id": work_id,
        "post_url": payload["url"], "field_count": len(payload["fields"]),
        "answer_keys": payload["answer_keys"],
        "response": data,
        "uploaded": prepared["uploaded"],
        "summary": (f"✅ 已提交（不可逆）：POST 到 {payload['url'][:70]}…，"
                    f"{len(payload['fields'])} 个字段；服务端返回 status="
                    f"{data.get('status')}"
                    + (f" msg={data.get('msg')}" if data.get("msg") else "")
                    + (f"；本次含附件 {len(prepared['uploaded'])} 个" if prepared["uploaded"] else "")),
    }


# ---------------------------------------------------------------- 批量（服务端循环，只回汇总不回全文）

def _batch_rows(items, fn) -> dict:
    """通用批量执行：逐项 catch，单项失败不影响其它；返回紧凑行。"""
    rows = []
    for item in items:
        wid = item.get("work_id") if isinstance(item, dict) else str(item)
        try:
            rows.append(fn(item))
        except Exception as e:  # noqa: BLE001
            rows.append({"work_id": wid, "status": "ERROR",
                         "error": f"{type(e).__name__}: {e}"})
    ok = sum(1 for r in rows if r.get("status") not in ("ERROR", "FAILED", "NEED_MORE_INFO"))
    failed = len(rows) - ok
    return {"count": len(rows), "ok": ok, "failed": failed, "rows": rows}


def batch_prepare_materials(*, work_ids: list[str], course_id: str = "",
                            root: str = DEFAULT_ROOT, refresh: bool = False) -> dict:
    """批量取材料：逐个落盘 state/materials/<work_id>.json，**只回清单+路径，不回全文**。

    这样主 agent 不会因为 N 份材料全文而撑爆上下文；真正的分析交给各子 agent 自己读。
    """
    def one(wid: str) -> dict:
        res = get_homework_materials(work_id=wid, course_id=course_id, root=root, refresh=refresh)
        return {
            "work_id": wid, "status": res.get("status"),
            "homework_name": res.get("homework_name"), "folder": res.get("folder"),
            "file_count": len(res.get("files") or []),
            "documents": [d.get("file") for d in (res.get("documents") or [])],
            "template_suggestion": (res.get("template_suggestion") or {}).get("file"),
            "personal_fields": res.get("personal_fields") or [],
            "warnings": res.get("warnings") or [],
            "reasons": res.get("reasons") or [],
            "materials_path": res.get("materials_path"),
        }

    out = _batch_rows([str(w) for w in work_ids], one)
    out["summary"] = (f"批量取材料：{out['count']} 个作业，成功 {out['ok']}、失败/需补充 {out['failed']}；"
                      "全文已分别落盘，请让各子 agent 自行读取（勿把全文塞回上下文）")
    return out


def batch_apply_edits(*, items: list[dict], root: str = DEFAULT_ROOT) -> dict:
    """批量回填 DOCX。items = [{work_id, edits, user_fields?, template?, overwrite?}]"""
    def one(item: dict) -> dict:
        res = apply_homework_edits(
            work_id=str(item["work_id"]), edits=item.get("edits") or [],
            template=str(item.get("template") or ""), user_fields=item.get("user_fields"),
            root=root, output_dir=str(item.get("output_dir") or ""),
            overwrite=bool(item.get("overwrite")))
        return {
            "work_id": str(item["work_id"]),
            "status": "OK" if not res.get("not_found") else "PARTIAL",
            "output": res.get("output"), "file_size": res.get("file_size"),
            "applied_count": res.get("applied_count"), "not_found": res.get("not_found"),
            "refused_personal": res.get("refused_personal"),
            "needs_user": res.get("needs_user"), "insufficient": res.get("insufficient"),
        }

    out = _batch_rows(items, one)
    out["summary"] = (f"批量回填：{out['count']} 个作业，成功 {out['ok']}、失败 {out['failed']}；"
                      "需用户手填/待补的项在各行里，请汇总后转述")
    return out


def batch_prepare_submissions(*, work_ids: list[str], answer_qid: str = "",
                              files_by_work: dict | None = None, save_only: bool = False,
                              course_id: str = "", root: str = DEFAULT_ROOT,
                              payload_dir: str = "") -> dict:
    """批量准备提交：逐个组装 payload 并**落盘** state/submit/<work_id>.payload.json。"""
    import json as _json

    files_by_work = files_by_work or {}

    def one(wid: str) -> dict:
        res = prepare_homework_submission(
            work_id=wid, files=files_by_work.get(wid), answer_qid=answer_qid,
            save_only=save_only, course_id=course_id, root=root)
        base = Path(payload_dir) if payload_dir else SUBMIT_CACHE_DIR
        path = base / f"{wid}.payload.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({"post_url": res["post_url"], "method": res["method"],
                                     "fields": res["fields_full"], "mode": res["mode"],
                                     "note": res["note"]}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return {"work_id": wid, "status": "READY_TO_SUBMIT", "mode": res["mode"],
                "post_url": res["post_url"], "answer_keys": res["answer_keys"],
                "field_count": res["field_count"], "uploaded": res["uploaded"],
                "payload_path": str(path)}

    out = _batch_rows([str(w) for w in work_ids], one)
    out["summary"] = (f"批量准备提交：{out['count']} 个作业，就绪 {out['ok']}、失败 {out['failed']}；"
                      "payload 已分别落盘（主 agent 审阅后再带 confirm 提交）")
    return out


def batch_submit(*, work_ids: list[str], confirm: bool = False, course_id: str = "",
                 root: str = DEFAULT_ROOT, review_tokens: dict | None = None) -> dict:
    """批量提交（**不可逆**）。confirm=False 时只回显将提交什么，零请求。

    每个 work_id 都必须带自己的 review_token（来自 prepare_homework_submission）；
    缺 token 的那一项**只报错、不提交**，其它项照常。
    """
    tokens = {str(k): str(v) for k, v in (review_tokens or {}).items()}

    def one(wid: str) -> dict:
        res = submit_homework(work_id=wid, confirm=confirm, course_id=course_id,
                              review_token=tokens.get(wid, ""))
        return {"work_id": wid,
                "status": "SUBMITTED" if res.get("submitted") else "READY_TO_SUBMIT",
                "submitted": bool(res.get("submitted")),
                "post_url": res.get("post_url"), "answer_keys": res.get("answer_keys"),
                "response": res.get("response")}

    out = _batch_rows([str(w) for w in work_ids], one)
    if not confirm:
        out["summary"] = (f"[未提交] 批量预览：{out['count']} 个作业已组装 payload，**未发送任何提交请求**；"
                          "要真提交请显式传 confirm=true，并且每个作业都要带上它自己的 review_token"
                          "（先 prepare → 把成品摆给用户审核 → 用户说提交之后再提交）")
    else:
        submitted = sum(1 for r in out["rows"] if r.get("submitted"))
        out["summary"] = f"批量提交：{out['count']} 个作业中成功提交 {submitted} 个，失败 {out['count']-submitted} 个（不可逆）"
    return out

# ---------------------------------------------------------------- 阶段一产物清点（只读，主 agent 用）

_IFRAME_OBJ_RE = re.compile(r'<iframe[^>]*\bobjectid="([^"]+)"[^>]*>', re.I)


def _attachments_in_fields(fields: dict) -> list[dict]:
    """从 payload 的答案正文里反解「这条 payload 会带上哪些附件」（离线，不发请求）。"""
    seen: dict[str, dict] = {}
    for key, val in (fields or {}).items():
        if not isinstance(val, str) or "objectid=" not in val:
            continue
        for m in _IFRAME_OBJ_RE.finditer(val):
            oid = m.group(1)
            if oid in seen:
                continue
            tag = m.group(0)
            fn = re.search(r'filename="([^"]*)"', tag)
            fs = re.search(r'filesize="([^"]*)"', tag)
            seen[oid] = {"objectid": oid,
                         "filename": _html_unescape(fn.group(1)) if fn else "",
                         "size_display": fs.group(1) if fs else "",
                         "answer_key": key}
    return list(seen.values())


def _folder_index(root: str) -> dict[str, dict]:
    """扫 <root>[/<账号>]/<作业>/_attachments.json，建 work_id → 作业目录 的**离线**索引。"""
    idx: dict[str, dict] = {}
    base = Path(root)
    if not base.is_dir():
        return idx
    manifests = list(base.glob("*/*/_attachments.json")) + list(base.glob("*/_attachments.json"))
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        wid = str(data.get("work_id") or "")
        if not wid:
            continue
        idx[wid] = {"homework_name": data.get("homework") or manifest.parent.name,
                    "course_id": str(data.get("course_id") or ""),
                    "status": data.get("status") or "",
                    "folder": str(manifest.parent)}
    return idx


def list_prepared_submissions(*, root: str = DEFAULT_ROOT,
                              work_ids: Optional[list[str]] = None) -> dict:
    """只读：清点「阶段一」已经做好的产物（payload / 完成版文档 / 将随 payload 一起交的附件）。

    **不联网、不提交、不写盘**。主 agent 用它在阶段一收尾时把成品摆给用户看，
    等用户说「提交」再用 submit_homework(confirm=true) 逐个真提交。
    """
    wanted = [str(w) for w in work_ids] if work_ids else None
    if wanted is None:
        wanted = sorted(p.name[: -len(".payload.json")]
                        for p in SUBMIT_CACHE_DIR.glob("*.payload.json"))
    index = _folder_index(root)
    items: list[dict] = []
    for wid in wanted:
        payload_path = SUBMIT_CACHE_DIR / f"{wid}.payload.json"
        cache = _load_submit_cache(wid)
        folder = index.get(wid, {}).get("folder", "") or cache.get("folder", "")
        item: dict = {
            "work_id": wid,
            "phase": "PHASE1_DONE" if payload_path.is_file() else "NO_PAYLOAD",
            "homework_name": index.get(wid, {}).get("homework_name")
                             or cache.get("homework_name", ""),
            "course_id": index.get(wid, {}).get("course_id", ""),
            "folder": folder,
            "submitted": False,
            "payload_path": "",
            "payload_mode": "", "py_flag": "", "post_url": "", "field_count": 0,
            "answer_keys": [], "attachments": [],
            "outputs": [], "needs_user_fields": [],
        }
        if folder and Path(folder).is_dir():
            item["outputs"] = sorted(str(f) for f in Path(folder).glob("*_完成版.docx"))
        if payload_path.is_file():
            try:
                data = json.loads(payload_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                item["phase"] = "PAYLOAD_UNREADABLE"
                item["error"] = str(exc)
                items.append(item)
                continue
            # 兼容两种落盘格式：{post_url, method, fields, ...}（本版本）与
            # {post_url, field_count, fields_full, uploaded, ...}（早期版本/子 agent 产出）
            fields = data.get("fields") or data.get("fields_full") or {}
            uploaded = data.get("uploaded")
            item.update({
                "payload_path": str(payload_path),
                "payload_mode": data.get("mode") or "",
                "py_flag": str(fields.get("pyFlag") or ""),
                "post_url": data.get("post_url") or "",
                "field_count": int(data.get("field_count") or len(fields)),
                "answer_keys": [str(k) for k in (data.get("answer_keys") or [])],
                "attachments": (
                    [{"objectid": str(u.get("objectid") or ""),
                      "filename": str(u.get("filename") or ""),
                      "size_display": "", "answer_key": ""} for u in uploaded]
                    if isinstance(uploaded, list) and uploaded
                    else _attachments_in_fields(fields)),
            })
            if data.get("page_url"):
                item["page_url"] = str(data["page_url"])
            if data.get("summary"):
                item["payload_summary"] = str(data["summary"])
        items.append(item)

    ready = [i for i in items if i["phase"] == "PHASE1_DONE"]
    blocked = [i for i in items if i["phase"] != "PHASE1_DONE"]
    parts = [f"阶段一产物清点：{len(items)} 个作业，payload 已就绪 {len(ready)} 个"]
    if blocked:
        parts.append("尚未就绪 " + str(len(blocked)) + " 个（"
                     + "；".join(f"{i['work_id']}={i['phase']}" for i in blocked) + "）")
    for i in ready:
        att = "、".join(a["filename"] for a in i["attachments"]) or "无"
        out = "、".join(Path(o).name for o in i["outputs"]) or "未找到完成版文档"
        parts.append(f"· {i['work_id']} {i['homework_name'] or '（名称未知）'}："
                     f"{i['payload_mode'] or '?'}，{i['field_count']} 字段，"
                     f"将交附件 {att}，成品 {out}")
    parts.append("**这些都是阶段一产物，尚未提交**：把上面的清单原文转述给用户、等他下命令；"
                 "他说『提交』才用 submit_homework(confirm=true) 逐个真提交，"
                 "说『重修/再改』则不要用本服务的工具。")
    return {"count": len(items), "ready_count": len(ready), "items": items,
            "summary": "\n".join(parts)}
