"""作业列表页解析的回归测试 —— 用真实页面 HTML 当 fixture。

fixture 来源：2026-10-13 用测试账号真实抓取的作业列表页（state/dump/*.html）。
钉住的关键行为：
  · 容器是 div.ulDiv li，导航 li 不能混进来
  · 两种作业形态都要认：A 可编辑(inspectTask+data) / B 已批阅(li.lookLi + href)
  · 状态取 <strong> 内的纯值（剥掉"作业状态："前缀）
  · 未完成严格口径只认「待做」
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xuexitong.attachments import parse_attachments, safe_name  # noqa: E402
from xuexitong.courses import Course  # noqa: E402
from xuexitong.homework import parse_homework_list, status_distribution  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
COURSE = Course(course_id="x", course_name="t", class_id="c", clazz_id="c", cpi="1", url="")


class TestHomeworkParse(unittest.TestCase):
    def load(self, name: str):
        return parse_homework_list((FIX / name).read_text(encoding="utf-8"), COURSE, "/work/getAllWork?a=1")

    def test_medicine_five_items_no_nav_junk(self):
        hws = self.load("list_medicine.html")
        self.assertEqual(len(hws), 5)                       # 真实页面就是 5 条
        names = [h.name for h in hws]
        for junk in ("首页", "任务", "统计", "待批作业"):
            self.assertNotIn(junk, names)                   # 导航项不能混进来

    def test_both_item_shapes_are_parsed(self):
        hws = {h.name: h for h in self.load("list_medicine.html")}
        a = hws["影像分割示例（2）"]                       # 形态 A：inspectTask + data
        self.assertEqual(a.work_id, "55200010")
        self.assertTrue(a.detail_url.startswith("/") or "doHomeWorkNew" in a.url)
        b = hws["影像增强示例（1）"]                       # 形态 B：li.lookLi + href
        self.assertEqual(b.work_id, "55200007")
        self.assertEqual(b.work_answer_id, "55200003")
        self.assertIn("selectWorkQuestionYiPiYue", b.detail_url)

    def test_status_value_is_clean(self):
        hws = self.load("list_medicine.html")
        dist = status_distribution(hws)
        self.assertEqual(dist, {"待批阅": 4, "已完成": 1})   # 没有"作业状态："前缀残留
        for h in hws:
            self.assertNotIn("作业状态", h.status_raw)

    def test_status_mapping_follows_chaoxing_rules(self):
        hws = self.load("list_medicine.html")
        done = [h for h in hws if h.name == "影像增强示例（1）"][0]
        self.assertEqual(done.status_num, 2)
        self.assertFalse(done.is_unfinished_strict)
        self.assertEqual(len([h for h in hws if h.is_unfinished_strict]), 0)   # 无待做

    def test_cloud_page(self):
        hws = self.load("list_cloud.html")
        self.assertEqual(len(hws), 2)
        self.assertEqual(status_distribution(hws), {"待批阅": 2})


class TestAttachments(unittest.TestCase):
    HTML = """
    <html><body>
      <div class="editor-iframe"><iframe objectid="aaa111" filename="实验报告.docx"></iframe></div>
      <div class="editor-iframe"><iframe objectid="bbb222" filename="数据.sql"></iframe></div>
      <div class="editor-iframe"><iframe objectid="aaa111" filename="重复.docx"></iframe></div>
      <iframe objectid="ccc333" src="x"></iframe>
      <div class="other"><iframe src="y"></iframe></div>
    </body></html>
    """

    def test_parse_attachments_dedup_and_fallback(self):
        atts = parse_attachments(self.HTML)
        self.assertEqual([a.object_id for a in atts], ["aaa111", "bbb222", "ccc333"])
        self.assertTrue(atts[0].url.endswith("/mooc-ans/ueditorupload/read?objectId=aaa111"))
        for a in atts:
            self.assertTrue(a.url)

    def test_parse_attachments_empty_page(self):
        self.assertEqual(parse_attachments("<html><body>没有附件</body></html>"), [])

    def test_safe_name_windows_and_chinese(self):
        self.assertEqual(safe_name("实验四 简单查询"), "实验四 简单查询")
        self.assertEqual(safe_name('a<b>c:d/e\\f|g?h*i'), "a_b_c_d_e_f_g_h_i")
        self.assertEqual(safe_name("  末尾空格. "), "末尾空格")
        self.assertEqual(safe_name(""), "未命名")
        self.assertLessEqual(len(safe_name("x" * 300)), 120)

    def test_safe_name_keeps_extension(self):
        n = safe_name("很长" * 80 + ".docx")
        self.assertTrue(n.endswith(".docx"))


class TestNotAListPageIsAnError(unittest.TestCase):
    """200 OK 但拿到的不是作业列表页时，必须报错 —— 不能当成"没有作业"。

    实测踩过：换账号后第一次请求拿到 HTTP 200 的风控验证页，
    被解析成 0 条作业，用户看到的是"这门课没有作业"。
    """

    def _client(self, body: str, status: int = 200):
        import httpx

        return httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text=body, headers={"content-type": "text/html"})))

    def test_verification_page_raises_fetch_error(self):
        from xuexitong.courses import FetchError
        from xuexitong.homework import fetch_homework_list
        menu = type("M", (), {"effective_url": "/work/getAllWork?x=1", "data": "", "href": "", "name": "作业"})
        client = self._client("<html><body>请完成验证后继续</body></html>")
        try:
            with self.assertRaises(FetchError) as ctx:
                fetch_homework_list(client, COURSE, menu)
            self.assertIn("不是作业列表页", str(ctx.exception))
        finally:
            client.close()

    def test_genuinely_empty_list_is_not_an_error(self):
        """真·空列表页含 ulDiv（只是没有条目）—— 这要算"没有作业"，不能报错。"""
        from xuexitong.homework import fetch_homework_list
        menu = type("M", (), {"effective_url": "/work/getAllWork?x=1", "data": "", "href": "", "name": "作业"})
        client = self._client('<div class="ulDiv"><ul class="clearfix"></ul></div>')
        try:
            items, _ = fetch_homework_list(client, COURSE, menu)
            self.assertEqual(items, [])
        finally:
            client.close()

    def test_antispider_body_with_200_raises_antispider_error(self):
        from xuexitong.courses import AntispiderError
        from xuexitong.homework import fetch_homework_list
        menu = type("M", (), {"effective_url": "/work/getAllWork?x=1", "data": "", "href": "", "name": "作业"})
        client = self._client('<html><div class="ulDiv"></div>antispiderShowVerify.ac</html>')
        try:
            with self.assertRaises(AntispiderError):
                fetch_homework_list(client, COURSE, menu)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
