# -*- coding: utf-8 -*-
"""新版作业列表 → 每作业 enc 的纯 HTTP 发现链路（离线测试）。

用 httpx.MockTransport 造三段响应：
  ① 新版课程页（含 #workEnc）  ② 作业列表页（每个作业带自己的 enc）
  ③ task 链接（一个跳到 dowork=可作答，一个跳到 view=已截止）
断言：可作答的写进 pages.json、已截止的如实标 view 且不写；解析函数的行为全部钉住。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from xuexitong import tasks  # noqa: E402
from xuexitong import worklist as wl  # noqa: E402

WORK_ENC = "1" * 32
ENC_OK = "a" * 32
ENC_CLOSED = "b" * 32
STU_URL = ("https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=111&clazzid=222"
           "&cpi=333&enc=" + "c" * 32 + "&t=1700000000000&pageHeader=8&v=0&hideHead=0")

STU_HTML = f'<html><input type="hidden" id="workEnc" name="workEnc" value="{WORK_ENC}"></html>'

LIST_HTML = (
    '<html><a data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=111&amp;classId=222'
    f'&amp;cpi=333&amp;workId=900&amp;answerId=901&amp;enc={ENC_OK}" onclick="goTask(this)">作业A 待批阅</a>'
    '<a data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=111&amp;classId=222'
    f'&amp;cpi=333&amp;workId=800&amp;answerId=801&amp;enc={ENC_CLOSED}" onclick="goTask(this)">作业B 已过期</a>'
    "</html>"
)

ANSWER_HTML = ("<html><input type='hidden' id=\"uploadEnc\" value=\"U\">"
               "<input type='hidden' id=\"uploadTimeStamp\" value=\"1\">"
               "<input type='hidden' id=\"userId\" value=\"9\">"
               "<form id=\"submitForm\" action=\"/a\"></form></html>")


def _client_menu_route() -> httpx.Client:
    """只给课程菜单参数（enc/openc）的客户端 —— 页面内容与 _client 相同。"""
    return _client()


def _client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "mycourse/stu" in u:
            return httpx.Response(200, text=STU_HTML)
        if "/mooc2/work/list" in u:
            return httpx.Response(200, text=LIST_HTML)
        if "work/task" in u:                       # 站点在这里 302 到 dowork 或 view
            tail = "work/dowork" if "workId=900" in u else "work/view"
            return httpx.Response(302, headers={"Location": u.replace("work/task", tail)})
        if "work/dowork" in u:
            return httpx.Response(200, text=ANSWER_HTML)
        if "work/view" in u:
            return httpx.Response(200, text="<html>作业详情</html>")
        if "isExpire" in u:
            return httpx.Response(200, json={"status": 0, "standardEnc": "S" * 32})
        return httpx.Response(404, text="?")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class ParseTest(unittest.TestCase):
    def test_parse_work_enc_and_encs(self):
        self.assertEqual(wl.parse_work_enc(STU_HTML), WORK_ENC)
        works = wl.parse_work_encs(LIST_HTML)
        self.assertEqual(set(works), {"900", "800"})
        self.assertEqual(works["900"]["answer_id"], "901")
        self.assertEqual(works["900"]["enc"], ENC_OK)
        self.assertNotIn("&amp;", works["900"]["task_url"])

    def test_parse_stu_url(self):
        info = wl.parse_stu_url(STU_URL)
        self.assertTrue(info["is_stu_page"])
        self.assertEqual((info["course_id"], info["class_id"], info["cpi"]), ("111", "222", "333"))
        self.assertEqual(info["enc"], "c" * 32)
        self.assertFalse(wl.parse_stu_url("https://mooc1.chaoxing.com/work/getAllWork?x=1")["is_stu_page"])

    def test_build_work_list_url_carries_all_params(self):
        u = wl.build_work_list_url(course_id="1", class_id="2", cpi="3", stuenc="S" * 32,
                                   work_enc="W" * 32, t=1700000000000)
        for part in ("courseId=1", "classId=2", "cpi=3", "ut=s", "t=1700000000000",
                     "stuenc=" + "S" * 32, "enc=" + "W" * 32):
            self.assertIn(part, u)

    def test_classify_task_url(self):
        self.assertEqual(wl.classify_task_url("https://x/mooc-ans/mooc2/work/dowork?a=1"), "dowork")
        self.assertEqual(wl.classify_task_url("https://x/mooc-ans/mooc2/work/view?a=1"), "view")
        self.assertEqual(wl.classify_task_url("https://x/other"), "other")


class MenuRouteTest(unittest.TestCase):
    """① course-menu(enc/openc) → studentcourse?v=2&fromMiddle=1 → #workEnc → ② 作业列表。"""

    def test_build_studentcourse_url_needs_v2_and_from_middle(self):
        u = wl.build_studentcourse_url(course_id="111", class_id="222", cpi="333",
                                       enc="E" * 32, openc="O" * 32)
        for part in ("mycourse/studentcourse", "courseId=111", "clazzid=222", "cpi=333",
                     "enc=" + "E" * 32, "openc=" + "O" * 32, "v=2", "fromMiddle=1"):
            self.assertIn(part, u)

    def test_parse_menu_params(self):
        url = ("/work/getAllWork?classId=222&courseId=111&isdisplaytable=2&mooc=1&ut=s"
               "&enc=" + "E" * 32 + "&cpi=333&openc=" + "O" * 32)
        p = wl.parse_menu_params(url)
        self.assertEqual((p["enc"], p["openc"], p["cpi"]), ("E" * 32, "O" * 32, "333"))

    def test_discover_directly_from_menu_params(self):
        client = _client_menu_route()
        self.addCleanup(client.close)
        out = wl.discover_course_encs(client, course_id="111", class_id="222", cpi="333",
                                      enc="E" * 32, openc="O" * 32)
        self.assertEqual(out["source"], "course-menu")
        self.assertEqual(set(out["works"]), {"900", "800"})
        self.assertEqual(out["works"]["900"]["enc"], ENC_OK)

    def test_auto_discover_needs_no_registered_url(self):
        """零用户输入：只要菜单能给 enc/openc，就能自动拿到作业 enc。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        empty_cache = wl.StuPageCache(Path(tmp.name) / "stu_pages.json")
        match = mock.Mock(course_id="111", class_id="222", cpi="333", work_id="900")
        with mock.patch.object(tasks, "_menu_params",
                               return_value={"enc": "E" * 32, "openc": "O" * 32, "cpi": "333"}), \
             mock.patch.object(tasks, "STU_PAGES", empty_cache), \
             mock.patch.object(tasks, "get_client", side_effect=_client):
            enc, note = tasks._auto_discover_enc(match)
        self.assertEqual(enc, ENC_OK)
        self.assertEqual(note, "")


class StuCacheAccountScopeTest(unittest.TestCase):
    """票据缓存必须按账号隔离：账号一变就作废，不能当全局配置用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = wl.StuPageCache(Path(self.tmp.name) / "stu_pages.json")

    def test_same_account_roundtrip(self):
        self.cache.put(course_id="111", class_id="222", stu_url=STU_URL, account="u1")
        self.assertEqual(self.cache.get(course_id="111", class_id="222", account="u1"), STU_URL)

    def test_other_account_gets_nothing_and_is_told_why(self):
        self.cache.put(course_id="111", class_id="222", stu_url=STU_URL, account="u1")
        self.assertEqual(self.cache.get(course_id="111", class_id="222", account="u2"), "")
        self.assertIn("属于账号 u1", self.cache.last_note)
        self.assertIn("重新 register_study_page", self.cache.last_note)

    def test_legacy_unscoped_file_is_discarded(self):
        path = Path(self.tmp.name) / "stu_pages.json"
        path.write_text(json.dumps({"111:222": {"stu_url": STU_URL}}, ensure_ascii=False),
                        encoding="utf-8")
        cache = wl.StuPageCache(path)
        self.assertEqual(cache.get(course_id="111", class_id="222", account="u1"), "")
        self.assertIn("旧格式", cache.last_note)


class RegisterFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pages = base / "submit" / "pages.json"
        patches = [
            mock.patch.object(tasks, "STU_PAGES", wl.StuPageCache(base / "stu_pages.json")),
            mock.patch.object(tasks, "PAGES_JSON", self.pages),
            mock.patch.object(tasks, "get_client", side_effect=_client),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_register_reports_dowork_and_closed_and_writes_pages(self):
        out = tasks.register_study_page(stu_url=STU_URL)
        kinds = {r["work_id"]: r["kind"] for r in out["works"]}
        self.assertEqual(kinds, {"900": "dowork", "800": "view"})
        self.assertIn("可作答 1 个", out["summary"])
        self.assertIn("已截止只有查看页 1 个", out["summary"])
        data = json.loads(self.pages.read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["900"])                    # 只记可作答的
        self.assertIn("account", out)                            # 票据按账号隔离
        self.assertEqual(data["900"]["enc"], ENC_OK)
        self.assertIn("workId=900", data["900"]["page_url"])
        self.assertIn("standardEnc=" + "S" * 32, data["900"]["page_url"])

    def test_answer_page_opens_from_auto_discovered_enc(self):
        tasks.register_study_page(stu_url=STU_URL)
        from xuexitong import submit
        client = _client()
        found = wl.discover_course_encs(client, course_id="111", class_id="222", cpi="333", stu_url=STU_URL)
        item = found["works"]["900"]
        url = submit.build_answer_page_url(course_id="111", class_id="222", cpi="333", work_id="900",
                                           answer_id=item["answer_id"], standard_enc="S" * 32,
                                           enc=item["enc"])
        page = submit.open_answer_page(client, course_id="111", class_id="222", cpi="333", work_id="900",
                                       answer_id="901", standard_enc="S" * 32, page_url=url)
        self.assertTrue(page.tokens_ok)

    def test_rejects_non_stu_url(self):
        with self.assertRaises(tasks.TaskError) as ctx:
            tasks.register_study_page(stu_url="https://mooc1.chaoxing.com/work/getAllWork?x=1")
        self.assertIn("新版课程页", str(ctx.exception))

    def test_auto_discover_explains_when_nothing_is_available(self):
        """菜单拿不到票据、也没注册课程页时，必须说清原因而不是静默失败。"""
        match = mock.Mock(course_id="111", class_id="222", cpi="333", work_id="900")
        enc, note = tasks._auto_discover_enc(match)
        self.assertEqual(enc, "")
        self.assertIn("课程菜单里没拿到", note)

    def test_auto_discover_flags_closed_work(self):
        tasks.register_study_page(stu_url=STU_URL)
        match = mock.Mock(course_id="111", class_id="222", cpi="333", work_id="800")
        enc, note = tasks._auto_discover_enc(match)
        self.assertEqual(enc, "")
        self.assertIn("已截止", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
