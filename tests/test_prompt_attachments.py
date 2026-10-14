"""题目内嵌附件的解析与下载。

实测形态（示例实验·VUE(一)）：作业的"题目正文"就是题目里内嵌的一份 docx，
它不在作业附件区 —— 目录扫描只会看到空目录。这里用合成 HTML + MockTransport 钉住：
  ① 只取**题干**里的附件，不碰**作答区**（否则会把自己交上去的附件当题目材料再下一遍）；
  ② 下载走 /ananas/status/<objectid> → download 直链，拿不到就退回 read 中转页。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from xuexitong.attachments import (  # noqa: E402
    Attachment, download_embedded, fetch_status_download_url, parse_prompt_attachments,
)

# 真实作答页的形状（已裁剪）：题干在 h3.mark_name 里，作答区在 .stem_answer 里，两边都有 editor-iframe。
PAGE_HTML = """<html><body>
<div class="questionLi" id="question777000777">
  <h3 class="mark_name">
    <p><br></p>
    <div class="editor-iframe">
      <iframe objectid="STEMOBJ" filename="示例实验·VUE(一).docx" filetype="docx"></iframe>
    </div>
    <p><br></p>
  </h3>
  <div class="stem_answer">
    <div class="editor-iframe">
      <iframe objectid="MYUPLOAD" filename="示例实验·VUE(一)_完成版.docx" filetype="docx"></iframe>
    </div>
  </div>
</div>
</body></html>"""

PAYLOAD = b"PK\x03\x04docx-bytes"


class ParsePromptAttachmentsTest(unittest.TestCase):
    def test_only_stem_attachment_is_taken(self):
        atts = parse_prompt_attachments(PAGE_HTML)
        self.assertEqual([a.object_id for a in atts], ["STEMOBJ"])
        self.assertEqual(atts[0].filename, "示例实验·VUE(一).docx")

    def test_answer_area_is_never_taken(self):
        """没有 mark_name 时也不能把作答区的附件当成题目材料。"""
        html = PAGE_HTML.replace('class="mark_name"', 'class="stem_tit"')
        ids = [a.object_id for a in parse_prompt_attachments(html)]
        self.assertNotIn("MYUPLOAD", ids)

    def test_empty_page_returns_nothing(self):
        self.assertEqual(parse_prompt_attachments("<html><body></body></html>"), [])


class DownloadEmbeddedTest(unittest.TestCase):
    def test_status_url_then_download(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if "/ananas/status/" in str(request.url):
                return httpx.Response(200, json={"status": "success",
                                                 "download": "https://d0.cldisk.com/download/STEMOBJ?at_=1"})
            return httpx.Response(200, content=PAYLOAD)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with TemporaryDirectory() as tmp:
            att = download_embedded(client, Attachment(object_id="STEMOBJ",
                                                      filename="示例实验·VUE(一).docx"), Path(tmp))
            content = Path(att.local_path).read_bytes() if att.local_path else b""
        client.close()
        self.assertTrue(any("/ananas/status/STEMOBJ" in u for u in seen), seen)
        self.assertTrue(any("d0.cldisk.com/download/STEMOBJ" in u for u in seen), seen)
        self.assertTrue(att.downloaded)
        self.assertEqual(att.size, len(PAYLOAD))
        self.assertEqual(content, PAYLOAD)

    def test_missing_download_field_falls_back_to_read_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/ananas/status/" in str(request.url):
                return httpx.Response(200, json={"status": "success"})   # 没有 download
            return httpx.Response(200, content=b"fallback-bytes")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with TemporaryDirectory() as tmp:
            att = download_embedded(client, Attachment(object_id="OBJ9", filename="a.docx"), Path(tmp))
            content = Path(att.local_path).read_bytes() if att.local_path else b""
        client.close()
        self.assertTrue(att.local_path.endswith("a.docx"))
        self.assertEqual(content, b"fallback-bytes")

    def test_status_endpoint_error_is_not_fatal(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
        self.assertEqual(fetch_status_download_url(client, "OBJ9"), "")
        self.assertEqual(fetch_status_download_url(client, ""), "")
        client.close()


if __name__ == "__main__":
    unittest.main()
