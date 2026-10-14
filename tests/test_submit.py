"""作答页解析 / 附件上传 / 提交 payload 的离线测试。

用**合成 fixture**（只含必要 id，不含任何真实账号数据）钉住行为，
断言里也复查"不会擅自提交"：prepare_payload 只组装，不发请求。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from xuexitong.submit import (  # noqa: E402
    PYFLAG_SAVE, PYFLAG_SUBMIT, SubmitError, build_answer_page_url, insert_html_for,
    parse_answer_page, prepare_payload, upload_attachment,
)

PAGE_URL = ("https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork?courseId=111&classId=222"
            "&cpi=333&workId=444&answerId=555&standardEnc=STANDARDENC&enc=NEWENC")

PAGE_HTML = """<html><body>
<form action="/mooc-ans/work/addStudentWorkNewWeb?_classId=222&amp;courseid=111&amp;token=TOKEN123"
      method="post" id="submitForm" name="submitForm">
  <input type="hidden" id="courseId" name="courseId" value="111">
  <input type="hidden" id="classId" name="classId" value="222">
  <input type="hidden" id="cpi" name="cpi" value="333">
  <input type="hidden" id="standardEnc" name="standardEnc" value="STANDARDENC">
  <input type="hidden" id="enc_work" name="enc_work" value="TOKEN123">
  <input type="hidden" id="pyFlag" name="pyFlag" value="">
  <input type="hidden" id="answerwqbid" name="answerwqbid" value="">
  <input type="hidden" id="answertype777" name="answertype777" value="4">
  <textarea name="answer777" id="answer777">已有作答内容</textarea>
</form>
<input type="hidden" id="uploadEnc" value="UPLOADENC">
<input type="hidden" id="uploadTimeStamp" value="1700000000000">
<input type="hidden" id="userId" value="999888">
<input type="hidden" id="addTimes" value="0">
<input type="hidden" id="limitWorkSubmitTimes" value="100">
<script>var isContentChanged = true;</script>
</body></html>"""


class ParsePageTest(unittest.TestCase):
    def test_parses_tokens_and_form(self):
        p = parse_answer_page(PAGE_HTML, PAGE_URL)
        self.assertTrue(p.tokens_ok)
        self.assertEqual(p.upload_enc, "UPLOADENC")
        self.assertEqual(p.upload_timestamp, "1700000000000")
        self.assertEqual(p.user_id, "999888")
        self.assertEqual(p.question_ids, ["777"])
        self.assertEqual(p.work_id, "444")
        self.assertEqual(p.answer_id, "555")
        self.assertEqual(p.standard_enc, "STANDARDENC")
        self.assertEqual(p.enc, "NEWENC")
        self.assertEqual(p.submit_limit, "100")
        # action 在 id 之前也要能取到，且 &amp; 要还原
        self.assertIn("addStudentWorkNewWeb", p.form_action)
        self.assertIn("token=TOKEN123", p.form_action)
        self.assertNotIn("&amp;", p.form_action)
        self.assertEqual(p.form_fields["courseId"], "111")
        self.assertEqual(p.form_fields["answer777"], "已有作答内容")

    def test_build_url(self):
        u = build_answer_page_url(course_id="1", class_id="2", cpi="3", work_id="4",
                                  answer_id="5", standard_enc="S", enc="E")
        for part in ("courseId=1", "classId=2", "cpi=3", "workId=4", "answerId=5",
                     "standardEnc=S", "enc=E"):
            self.assertIn(part, u)


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.page = parse_answer_page(PAGE_HTML, PAGE_URL)
        self.seen = {}

    def _client(self, payload: dict, status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen["url"] = str(request.url)
            body = request.read().decode("utf-8", errors="replace")
            self.seen["body"] = body
            self.seen["headers"] = dict(request.headers)
            return httpx.Response(status, json=payload)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_upload_sends_required_params_and_field_name(self):
        client = self._client({"state": "SUCCESS", "objectid": "OBJ1",
                               "original": "报告.docx", "fileType": ".docx", "size": 10,
                               "url": "/ueditorupload/read?objectId=OBJ1", "objectIdEnc": "E1"})
        f = upload_attachment(client, self.page, filename="报告.docx", data=b"1234567890")
        client.close()
        u = self.seen["url"]
        for part in ("ueditorupload/attachment", "uploadtype=work", "source=1",
                     "enc2=UPLOADENC", "t=1700000000000", "uid=999888"):
            self.assertIn(part, u)
        self.assertIn('name="upfile"', self.seen["body"])
        self.assertIn("报告.docx", self.seen["body"])
        self.assertEqual(f.objectid, "OBJ1")
        self.assertEqual(f.size, 10)
        self.assertIn('objectid="OBJ1"', f.insert_html)
        self.assertIn('filename="报告.docx"', f.insert_html)
        self.assertIn("insertCloud.html", f.insert_html)

    def test_no_permission_is_a_clear_error(self):
        client = self._client({"state": "no permission", "status": False})
        with self.assertRaises(SubmitError) as ctx:
            upload_attachment(client, self.page, filename="a.docx", data=b"x")
        client.close()
        self.assertIn("no permission", str(ctx.exception))

    def test_missing_tokens_raises(self):
        empty = parse_answer_page("<html></html>")
        with self.assertRaises(SubmitError):
            upload_attachment(httpx.Client(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={}))), empty, filename="a.docx", data=b"x")


class PreparePayloadTest(unittest.TestCase):
    def test_payload_uses_pyflag_3_and_keeps_existing_answers(self):
        page = parse_answer_page(PAGE_HTML, PAGE_URL)
        p = prepare_payload(page)
        self.assertEqual(p["py_flag"], PYFLAG_SUBMIT)
        self.assertIn("pyFlag=3", p["url"])
        self.assertIn("token=TOKEN123", p["url"])
        self.assertIn("saveStatus=1", p["url"])
        self.assertEqual(p["fields"]["pyFlag"], PYFLAG_SUBMIT)
        self.assertEqual(p["fields"]["answer777"], "已有作答内容")   # 不传 answers 时沿用原值
        self.assertEqual(p["method"], "post")

    def test_payload_fills_answerwqbid_with_question_ids(self):
        """站点 ready2Submit() 会把 #questionIds 写成 "777,"；漏掉会得到 无效的参数：code-1！"""
        page = parse_answer_page(PAGE_HTML, PAGE_URL)
        for py in (PYFLAG_SUBMIT, PYFLAG_SAVE):
            p = prepare_payload(page, py_flag=py)
            self.assertEqual(p["fields"]["answerwqbid"], "777,")

    def test_payload_can_override_answer_with_attachment_html(self):
        page = parse_answer_page(PAGE_HTML, PAGE_URL)
        html = insert_html_for(_Fake("OBJ9", "报告.docx", ".docx", 123))
        p = prepare_payload(page, answers={"777": html})
        self.assertIn('objectid="OBJ9"', p["fields"]["answer777"])
        self.assertEqual(p["answer_keys"], ["answer777"])

    def test_answers_keys_must_be_question_ids(self):
        """answers 的键必须是题目 id：传字段名（历史事故）要当场报错，不能静默发垃圾字段。"""
        page = parse_answer_page(PAGE_HTML, PAGE_URL)
        with self.assertRaises(SubmitError) as ctx:
            prepare_payload(page, answers={"courseId": "111"})
        self.assertIn("题目 id", str(ctx.exception))
        # 题目 id 本身没问题
        self.assertEqual(prepare_payload(page, answers={"777": "x"})["fields"]["answer777"], "x")

    def test_save_mode_uses_pyflag_1(self):
        page = parse_answer_page(PAGE_HTML, PAGE_URL)
        p = prepare_payload(page, py_flag=PYFLAG_SAVE)
        self.assertIn("pyFlag=1", p["url"])
        self.assertEqual(p["fields"]["pyFlag"], "1")

    def test_payload_never_sends_by_itself(self):
        """prepare_payload 必须是纯函数：不产生任何网络请求。"""
        calls = []
        page = parse_answer_page(PAGE_HTML, PAGE_URL)
        p = prepare_payload(page, answers={"777": "x"})
        self.assertTrue(p["url"].startswith("/mooc-ans/work/addStudentWorkNewWeb"))
        self.assertEqual(calls, [])

    def test_missing_action_raises(self):
        page = parse_answer_page("<html><input id='courseId' value='1'></html>")
        with self.assertRaises(SubmitError):
            prepare_payload(page)


class _Fake:
    def __init__(self, objectid, filename, filetype, size):
        self.objectid, self.filename, self.filetype, self.size = objectid, filename, filetype, size


class InsertHtmlFidelityTest(unittest.TestCase):
    """与站点 uploadCommon.js 的输出对齐：filesize 是显示值、name= 是 base64(URL(JSON)）。"""

    def test_human_size_matches_site_sample(self):
        from xuexitong.submit import human_size
        self.assertEqual(human_size(23104), "22.56K")     # 站点上真实附件显示为 22.56K
        self.assertEqual(human_size(36743), "35.88K")

    def test_name_param_roundtrip(self):
        import base64
        import json
        import re
        from urllib.parse import unquote

        from xuexitong.submit import UploadedFile, insert_html_for
        f = UploadedFile(objectid="OBJ9", filename="报告.docx", filetype=".docx", size=23104)
        h = insert_html_for(f)
        self.assertIn('filesize="22.56K"', h)
        m = re.search(r'\sname="([^"]*)"', h)
        self.assertIsNotNone(m)
        meta = json.loads(unquote(base64.b64decode(m.group(1)).decode()))
        self.assertEqual(meta, {"objectid": "OBJ9", "name": "报告.docx", "type": "docx", "size": "22.56K"})


class SubmitGateTest(unittest.TestCase):
    """confirm 门禁：没有 confirm=true 就**一个请求都不发**；有 confirm 才按 payload POST。

    全程用 httpx.MockTransport，不会真的碰学习通。
    """

    def setUp(self):
        self.page = parse_answer_page(PAGE_HTML, PAGE_URL)
        self.requests: list[tuple] = []

    def _install(self, page=None, response=None):
        from unittest import mock as umock

        from xuexitong import tasks as t

        page = page or self.page
        payload = response if response is not None else {"status": True, "msg": ""}

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append((request.method, str(request.url),
                                  request.read().decode("utf-8", errors="replace")))
            return httpx.Response(200, json=payload)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        for patch in (umock.patch.object(t, "_resolve_submit_page", return_value=(page, {})),
                      umock.patch.object(t, "get_client", return_value=client)):
            patch.start()
            self.addCleanup(patch.stop)
        return self.requests

    def _token(self, wid: str = "444") -> str:
        """先走一遍 prepare（=把成品摆给用户的那一步）拿到 review_token。"""
        from xuexitong import tasks as t
        return t.prepare_homework_submission(work_id=wid)["review_token"]

    def test_confirm_without_review_token_is_blocked(self):
        """铁律：完成与提交不能一步做完 —— 没有 review_token ≈ 没给用户看过 → 零请求拒绝。"""
        from xuexitong import tasks as t
        reqs = self._install()
        with self.assertRaises(t.TaskError) as ctx:
            t.submit_homework(work_id="444", confirm=True)
        self.assertIn("先展示、后提交", str(ctx.exception))
        self.assertIn("review_token", str(ctx.exception))
        self.assertEqual(reqs, [])                       # ← 核心断言：零请求

    def test_stale_review_token_is_blocked(self):
        """内容变过（token 不匹配）→ 必须重新展示给用户，不许直接提交。"""
        from xuexitong import tasks as t
        reqs = self._install()
        with self.assertRaises(t.TaskError) as ctx:
            t.submit_homework(work_id="444", confirm=True, review_token="rv-deadbeefdeadbeef")
        self.assertIn("不匹配", str(ctx.exception))
        self.assertEqual(reqs, [])

    def test_without_confirm_sends_nothing(self):
        from xuexitong import tasks as t
        reqs = self._install()
        res = t.submit_homework(work_id="444", confirm=False)
        self.assertFalse(res["submitted"])
        self.assertTrue(res["confirm_required"])
        self.assertEqual(reqs, [])                       # ← 核心断言：零请求
        self.assertIn("confirm=true", res["summary"])
        self.assertIn("answer777", res["answer_keys"])
        self.assertTrue(res["fields_full"]["answer777"])

    def test_with_confirm_posts_pyflag_3(self):
        from xuexitong import tasks as t
        reqs = self._install()
        token = self._token()
        res = t.submit_homework(work_id="444", confirm=True, review_token=token)
        self.assertTrue(res["submitted"])
        self.assertEqual(len(reqs), 1)
        method, url, body = reqs[0]
        self.assertEqual(method, "POST")
        self.assertIn("addStudentWorkNewWeb", url)
        self.assertIn("pyFlag=3", url)
        self.assertIn("answer777", body)
        self.assertIn("pyFlag=3", body)
        self.assertIn("saveStatus=1", url)
        self.assertIn("已提交", res["summary"])

    def test_guard_blocks_when_submit_limit_exceeded(self):
        """addTimes > limit 时站点会要图形验证码 → 必须拒绝，且不发提交请求。"""
        from xuexitong import tasks as t
        page = parse_answer_page(PAGE_HTML.replace('id="addTimes" value="0"',
                                                  'id="addTimes" value="200"'), PAGE_URL)
        reqs = self._install(page=page)
        token = self._token()
        with self.assertRaises(t.TaskError) as ctx:
            t.submit_homework(work_id="444", confirm=True, review_token=token)
        self.assertIn("验证码", str(ctx.exception))
        self.assertEqual(reqs, [])                       # 守卫拦住，零请求

    def test_server_rejection_surfaces_message(self):
        from xuexitong import tasks as t
        self._install(response={"status": False, "msg": "作业已截止，无法提交"})
        token = self._token()
        with self.assertRaises(Exception) as ctx:
            t.submit_homework(work_id="444", confirm=True, review_token=token)
        self.assertIn("作业已截止", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=1)
