"""作业作答页 / 附件上传 / 提交 payload（**本模块只准备，不擅自提交**）。

实测链路（2026-10-13，新版 mooc2 作答页）：

  作答页  GET https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork
             ?courseId&classId&cpi&workId&answerId&standardEnc&enc
          · standardEnc 可由 GET /mooc-ans/work/isExpire?classId&workRelationId&cpi&courseId 取（status==0）
          · enc **是新版页面各自下发的**：用老列表页的 enc 会 403（实测）
          · 页面静态 HTML 里就有 #uploadEnc / #uploadTimeStamp / #userId / #submitForm action

  附件上传 POST https://mooc1.chaoxing.com/upload-ans/ueditorupload/attachment
             ?uploadtype=work&source=1&enc2=<#uploadEnc>&t=<#uploadTimeStamp>&uid=<#userId>
           multipart 字段名 upfile
           → {"state":"SUCCESS","objectid":..,"original":..,"fileType":..,"size":..,"url":..}

  提交   POST <作答页里 #submitForm 的 action>（token/totalQuestionNum 都在里面）
          表单字段 = #submitForm.serialize()，并追加 &ua=&formType=&saveStatus=1&version=N
          保存 vs 提交的判别字段：#pyFlag（保存=1，提交=3）
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

UPLOAD_URL = "https://mooc1.chaoxing.com/upload-ans/ueditorupload/attachment"
DOWORK_URL = "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork"
ISEXPIRE_URL = "https://mooc1-2.chaoxing.com/mooc-ans/work/isExpire"
INSERT_IFRAME_SRC = "/ananas/common-modules/attachment/insertCloud.html"

PYFLAG_SAVE = "1"
PYFLAG_SUBMIT = "3"

MIME_BY_SUFFIX = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".rar": "application/vnd.rar",
    ".txt": "text/plain",
}

__all__ = [
    "SubmitError", "UploadedFile", "AnswerPage",
    "parse_answer_page", "build_answer_page_url", "get_standard_enc", "open_answer_page",
    "upload_attachment", "insert_html_for", "prepare_payload", "human_size", "name_param_for",
    "check_submit_guard", "post_submission",
]


class SubmitError(RuntimeError):
    """作答页/上传/提交准备阶段的可预期失败。"""


@dataclass
class UploadedFile:
    objectid: str
    filename: str
    filetype: str = ""
    size: int = 0
    url: str = ""
    object_id_enc: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def insert_html(self) -> str:
        return insert_html_for(self)


@dataclass
class AnswerPage:
    url: str = ""
    course_id: str = ""
    class_id: str = ""
    cpi: str = ""
    work_id: str = ""
    answer_id: str = ""
    standard_enc: str = ""
    enc: str = ""
    upload_enc: str = ""
    upload_timestamp: str = ""
    user_id: str = ""
    question_ids: list[str] = field(default_factory=list)
    add_times: str = ""
    submit_limit: str = ""
    form_action: str = ""
    form_fields: dict[str, str] = field(default_factory=dict)
    html_len: int = 0

    @property
    def tokens_ok(self) -> bool:
        return bool(self.upload_enc and self.upload_timestamp and self.user_id)


def _hidden(html: str, name: str) -> str:
    m = re.search(r'<(?:input|textarea)[^>]*id="' + name + r'"[^>]*value="([^"]*)"', html)
    if m:
        return _html.unescape(m.group(1))
    m = re.search(r'<(?:input|textarea)[^>]*id="' + name + r'"[^>]*>([^<]*)</', html)
    return _html.unescape(m.group(1)) if m else ""


def _form_fields(html: str) -> dict[str, str]:
    """抓 #submitForm 里的具名字段（用于 prepare_payload 的表单体）。"""
    m = re.search(r'<form[^>]*id="submitForm"[\s\S]*?</form>', html)
    block = m.group(0) if m else html
    fields: dict[str, str] = {}
    for t in re.finditer(r"<input[^>]*>", block):
        name = re.search(r'name="([^"]+)"', t.group(0))
        if not name:
            continue
        val = re.search(r'value="([^"]*)"', t.group(0))
        fields[name.group(1)] = _html.unescape(val.group(1)) if val else ""
    # 答案正文通常在 <textarea name="answer<qid>">…</textarea> 里
    for t in re.finditer(r"<textarea[^>]*name=\"([^\"]+)\"[^>]*>([\s\S]*?)</textarea>", block, re.I):
        fields[t.group(1)] = _html.unescape(t.group(2))
    return fields


def parse_answer_page(html: str, url: str = "") -> AnswerPage:
    """从新版作答页 HTML 解析全部令牌与表单。"""
    page = AnswerPage(url=url or "", html_len=len(html or ""))
    page.upload_enc = _hidden(html, "uploadEnc")
    page.upload_timestamp = _hidden(html, "uploadTimeStamp")
    page.user_id = _hidden(html, "userId")
    page.course_id = _hidden(html, "courseId")
    page.class_id = _hidden(html, "classId")
    page.cpi = _hidden(html, "cpi")
    page.standard_enc = _hidden(html, "standardEnc")
    page.answer_id = _hidden(html, "workAnswerId") or _hidden(html, "answerId")
    page.add_times = _hidden(html, "addTimes")
    page.submit_limit = _hidden(html, "limitWorkSubmitTimes")
    if url:
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(url).query)
        page.course_id = page.course_id or (q.get("courseId") or [""])[0]
        page.class_id = page.class_id or (q.get("classId") or [""])[0]
        page.cpi = page.cpi or (q.get("cpi") or [""])[0]
        page.work_id = (q.get("workId") or [""])[0]
        page.answer_id = page.answer_id or (q.get("answerId") or [""])[0]
        page.standard_enc = page.standard_enc or (q.get("standardEnc") or [""])[0]
        page.enc = (q.get("enc") or [""])[0]
    page.question_ids = sorted(set(re.findall(r'id="answertype(\d+)"', html)))
    # 注意：form 标签里 action 可能在 id 之前/之后，必须整段抓再取属性
    m = re.search(r"<form[^>]*id=\"submitForm\"[^>]*>", html)
    if m:
        a = re.search(r'action="([^"]+)"', m.group(0))
        if a:
            page.form_action = _html.unescape(a.group(1))
    page.form_fields = _form_fields(html)
    return page


def build_answer_page_url(*, course_id: str, class_id: str, cpi: str, work_id: str,
                          answer_id: str, standard_enc: str, enc: str) -> str:
    from urllib.parse import urlencode

    q = {"courseId": course_id, "classId": class_id, "cpi": cpi, "workId": work_id,
         "answerId": answer_id, "standardEnc": standard_enc, "enc": enc}
    return f"{DOWORK_URL}?{urlencode(q)}"


def get_standard_enc(client: httpx.Client, *, course_id: str, class_id: str, cpi: str,
                     work_id: str) -> str:
    """GET /mooc-ans/work/isExpire → standardEnc（status==0 时有效）。"""
    try:
        r = client.get(ISEXPIRE_URL, params={"classId": class_id, "workRelationId": work_id,
                                             "cpi": cpi, "courseId": course_id}, timeout=30)
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return ""
    if isinstance(data, dict) and data.get("status") == 0:
        return str(data.get("standardEnc") or "")
    return ""


def open_answer_page(client: httpx.Client, *, course_id: str, class_id: str, cpi: str,
                     work_id: str, answer_id: str, standard_enc: str = "", enc: str = "",
                     page_url: str = "") -> AnswerPage:
    """打开新版作答页并解析。enc 必须来自新版 UI（老列表页的 enc 会 403）。"""
    if not page_url:
        if not enc:
            raise SubmitError(
                "缺少新版 enc：作答页需要 courseId/classId/cpi/workId/answerId/standardEnc/enc，"
                "其中 enc 由新版页面下发（用老版作业列表页的 enc 会 403）。"
                "请用浏览器打开该作业的作答页后，把地址栏 URL 通过 page_url 传入。"
            )
        page_url = build_answer_page_url(course_id=course_id, class_id=class_id, cpi=cpi,
                                         work_id=work_id, answer_id=answer_id,
                                         standard_enc=standard_enc, enc=enc)
    r = client.get(page_url, timeout=30)
    if r.status_code != 200:
        raise SubmitError(f"打开作答页失败：HTTP {r.status_code}（{len(r.text)} 字节）。"
                          "常见原因：enc 不是新版页面的、或该作答记录已失效。")
    page = parse_answer_page(r.text, str(r.url))
    if not page.tokens_ok:
        raise SubmitError(f"作答页里没有上传令牌（uploadEnc/uploadTimeStamp/userId 缺一）——"
                          f"页面 {len(r.text)} 字节，可能不是新版作答页。")
    return page


def upload_attachment(client: httpx.Client, page: AnswerPage, *, filename: str,
                      data: bytes, mime: str = "") -> UploadedFile:
    """把文件传到学习通的附件接口（只上传，不挂载、不保存、不提交）。"""
    if not page.tokens_ok:
        raise SubmitError("作答页令牌不全，无法上传（先调用 open_answer_page 成功）")
    suffix = Path(filename).suffix.lower()
    mime = mime or MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
    params = {"uploadtype": "work", "source": "1", "enc2": page.upload_enc,
              "t": page.upload_timestamp, "uid": page.user_id}
    r = client.post(UPLOAD_URL, params=params,
                    files={"upfile": (filename, data, mime)},
                    headers={"Referer": page.url or DOWORK_URL,
                             "Origin": "https://mooc1.chaoxing.com",
                             "X-Requested-With": "XMLHttpRequest"}, timeout=180)
    try:
        d = json.loads(r.text)
    except ValueError:
        raise SubmitError(f"上传响应不是 JSON：HTTP {r.status_code} {r.text[:200]}") from None
    if not isinstance(d, dict) or d.get("state") != "SUCCESS":
        raise SubmitError(f"上传失败：{json.dumps(d, ensure_ascii=False)[:300]}"
                          "（常见原因：#uploadTimeStamp 过期，重新打开作答页再传）")
    return UploadedFile(objectid=str(d.get("objectid", "")), filename=str(d.get("original") or filename),
                        filetype=str(d.get("fileType", suffix)), size=int(d.get("size") or len(data)),
                        url=str(d.get("url", "")), object_id_enc=str(d.get("objectIdEnc", "")), raw=d)


def human_size(n: int) -> str:
    """学习通自己用的是 22.56K 这种显示值（不是字节数）。"""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f}M"
    if n >= 1024:
        return f"{n / 1024:.2f}K"
    return f"{n}B"


def name_param_for(f: UploadedFile) -> str:
    """附件卡片里 name= 的那串参数（实测格式：base64(URL 编码(JSON))）。"""
    import base64
    from urllib.parse import quote

    payload = {"objectid": f.objectid, "name": f.filename,
               "type": (f.filetype or "").lstrip("."), "size": human_size(f.size)}
    return base64.b64encode(quote(json.dumps(payload, ensure_ascii=False)).encode()).decode()


def insert_html_for(f: UploadedFile) -> str:
    """上传成功后编辑器插入的那段 HTML —— 也就是提交时 answer<qid> 里需要的内容。

    严格对齐站点 uploadCommon.js 的输出：多一个 name= 参数（附件元数据 base64）。
    这是对比"用户已提交内容"时发现的保真度缺口：少了它，卡片回显可能对不上。
    """
    ft = (f.filetype or "").lstrip(".")
    return ('<div class="editor-iframe" contenteditable="false"><iframe frameborder="0" '
            'scrolling="no" src="' + INSERT_IFRAME_SRC + '" class="attach-iframe" '
            'module="insertAttach" objectid="' + f.objectid + '" filename="' + f.filename + '" '
            'filetype="' + ft + '" filesize="' + human_size(f.size) + '" name="' + name_param_for(f) + '" '
            'style="display: block; max-width: 620px;"></iframe></div>')


def prepare_payload(page: AnswerPage, *, answers: Optional[dict[str, str]] = None,
                    extra_fields: Optional[dict[str, str]] = None,
                    py_flag: str = PYFLAG_SUBMIT) -> dict:
    """组装提交/保存用的 payload（**只组装，不发请求**）。

    answers: {question_id: 该题的答案 HTML}；不传则沿用页面里已有的答案字段值。
    """
    if not page.form_action:
        raise SubmitError("作答页里没有 #submitForm 的 action，无法组装提交 payload")
    # answers 的键必须是**题目 id**（page.question_ids），不是表单字段名。
    # 历史上 submit_homework 把整份 fields_full 当 answers 传过：每个键都被写成
    # `fields["answer" + 键]`，于是真正的 answer<qid> 不被附件正文覆盖 ——
    # 提交上去是空答案，而站点照样回 {"status": true, "msg": "success!"}。
    # 这种"静默成功"比报错危险得多，所以这里直接拦。
    if answers and page.question_ids:
        unknown = [str(q) for q in answers if str(q) not in page.question_ids]
        if unknown:
            raise SubmitError(
                "answers 的键必须是题目 id（本题只有 %s），收到的是 %s —— "
                "想直接覆盖表单字段请改用 extra_fields。"
                % ("、".join(page.question_ids), "、".join(unknown[:5])))
    fields = dict(page.form_fields)
    # 站点自己的提交/保存 JS（ready2Submit()）会把 #questionIds（name=answerwqbid）
    # 写成 "题目id,题目id," —— 漏掉它站点直接回 {"status": false, "msg": "无效的参数：code-1！"}
    if page.question_ids:
        fields["answerwqbid"] = ",".join(page.question_ids) + ","
    fields["pyFlag"] = py_flag
    if answers:
        for qid, content in answers.items():
            fields[f"answer{qid}"] = content
    if extra_fields:
        fields.update(extra_fields)
    url = page.form_action
    for k, v in page.form_fields.items():          # action 里已带的参数不再重复
        pass
    action_has_py = "pyFlag=" in url
    if not action_has_py:
        url = url + ("&" if "?" in url else "?") + "pyFlag=" + py_flag
    url += "&ua=&formType=1&saveStatus=1&version=1"
    return {
        "url": url, "method": "post", "fields": fields,
        "answer_keys": [k for k in fields if re.fullmatch(r"answer\d+", k)],
        "py_flag": py_flag,
        "note": ("pyFlag=3 表示提交，=1 表示保存；这里的 saveStatus=1 两种模式都会带，"
                 "真正的判别字段是 pyFlag"),
    }


# ---------------------------------------------------------------- 真正提交（必须显式确认）

MOOC_HOST = "https://mooc1.chaoxing.com"


def check_submit_guard(page: AnswerPage) -> str:
    """提交前的本地守卫（对应站点的 submitCheckTimes）。

    addTimes > limitWorkSubmitTimes 时，站点会先弹图形验证码（showWorkVerify），
    纯 HTTP 无法过 —— 这时明确拒绝，让用户手动提交。
    返回 "" 表示可以继续。
    """
    try:
        add = int(page.add_times or 0)
        limit = int(page.submit_limit or 100)
    except (TypeError, ValueError):
        return ""
    if add > limit:
        return (f"该作业提交次数 {add} 已超过上限 {limit}，站点会要求先过图形验证码，"
                "纯 HTTP 无法完成 —— 请手动提交。")
    return ""


def post_submission(client: httpx.Client, payload: dict, *, referer: str = "") -> dict:
    """把 prepare_payload 产出的 payload 真正 POST 出去（**调用方必须先取得用户确认**）。"""
    url = payload.get("url") or ""
    if not url:
        raise SubmitError("payload 里没有 url")
    if url.startswith("/"):
        url = MOOC_HOST + url
    r = client.post(url, data=payload.get("fields") or {},
                    headers={"Referer": referer or DOWORK_URL,
                             "Origin": MOOC_HOST,
                             "X-Requested-With": "XMLHttpRequest"},
                    timeout=180)
    try:
        data = r.json()
    except ValueError:
        raise SubmitError(f"提交响应不是 JSON：HTTP {r.status_code} {r.text[:200]}") from None
    if not isinstance(data, dict):
        raise SubmitError(f"提交响应结构异常：{str(data)[:200]}")
    if not data.get("status"):
        raise SubmitError(f"提交被拒绝：{data.get('msg') or json.dumps(data, ensure_ascii=False)[:200]}")
    return data
