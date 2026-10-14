# -*- coding: utf-8 -*-
"""两段式流程的离线测试：阶段一产物清点（list_prepared_submissions）+ 编排契约文案。

全部离线：不联网、不写用户目录、不提交。
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

from xuexitong import tasks as t  # noqa: E402
from xuexitong.submit import INSERT_IFRAME_SRC, insert_html_for, UploadedFile  # noqa: E402


class PreparedListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state" / "submit"
        self.state.mkdir(parents=True)
        self.root = self.base / "学习通作业"

        # 作业目录 + 清单（模拟真实落盘布局 <root>/<账号>/<作业名>/）
        self.hw_folder = self.root / "100000002" / "需求分析说明书"
        self.hw_folder.mkdir(parents=True)
        (self.hw_folder / "软件工程需求分析实验报告_模板_完成版.docx").write_bytes(b"PK\x03\x04fake")
        (self.hw_folder / "_attachments.json").write_text(json.dumps({
            "homework": "需求分析说明书", "work_id": "44400044", "course_id": "111000111",
            "status": "待批阅", "attachments": [],
        }, ensure_ascii=False), encoding="utf-8")

        # payload（模拟 prepare_homework_submission 的落盘格式）
        f = UploadedFile(objectid="OBJ123", filename="需求分析说明书_完成版.docx",
                         filetype=".docx", size=23104)
        fields = {"courseId": "111000111", "pyFlag": "3", "answer215570344": insert_html_for(f)}
        (self.state / "44400044.payload.json").write_text(json.dumps({
            "work_id": "44400044", "saved_at": 1, "homework_name": "需求分析说明书",
            "post_url": "/mooc-ans/work/addStudentWorkNewWeb?token=T", "method": "post",
            "fields": fields, "mode": "提交(pyFlag=3)", "answer_keys": ["answer215570344"],
            "note": "",
        }, ensure_ascii=False), encoding="utf-8")

        self.patch = mock.patch.object(t, "SUBMIT_CACHE_DIR", self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)

        # 清点必须是纯离线的：一旦联网就炸
        self.net = mock.patch.object(
            t, "get_client",
            side_effect=AssertionError("list_prepared_submissions 不允许联网"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def test_reports_phase1_done_with_outputs_and_attachments(self):
        res = t.list_prepared_submissions(root=str(self.root), work_ids=["44400044"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["ready_count"], 1)
        item = res["items"][0]
        self.assertEqual(item["phase"], "PHASE1_DONE")
        self.assertFalse(item["submitted"])
        self.assertEqual(item["homework_name"], "需求分析说明书")
        self.assertEqual(item["py_flag"], "3")
        self.assertEqual(item["field_count"], 3)
        self.assertEqual(item["answer_keys"], ["answer215570344"])
        self.assertEqual([a["filename"] for a in item["attachments"]],
                         ["需求分析说明书_完成版.docx"])
        self.assertEqual([Path(o).name for o in item["outputs"]],
                         ["软件工程需求分析实验报告_模板_完成版.docx"])
        self.assertIn("阶段一", res["summary"])
        self.assertIn("尚未提交", res["summary"])

    def test_missing_payload_is_flagged_not_invented(self):
        res = t.list_prepared_submissions(root=str(self.root), work_ids=["1", "2"])
        self.assertEqual(res["ready_count"], 0)
        self.assertTrue(all(i["phase"] == "NO_PAYLOAD" for i in res["items"]))
        self.assertIn("尚未就绪 2 个", res["summary"])

    def test_no_work_ids_lists_all_payloads_on_disk(self):
        res = t.list_prepared_submissions(root=str(self.root))
        self.assertEqual([i["work_id"] for i in res["items"]], ["44400044"])

    def test_insert_html_parsing_matches_site_markup(self):
        html = insert_html_for(UploadedFile(objectid="OBJ9", filename="报告.docx",
                                            filetype=".docx", size=36743))
        self.assertIn(INSERT_IFRAME_SRC, html)
        found = t._attachments_in_fields({"answer1": html})
        self.assertEqual(found[0]["objectid"], "OBJ9")
        self.assertEqual(found[0]["filename"], "报告.docx")
        self.assertEqual(found[0]["size_display"], "35.88K")
        self.assertEqual(found[0]["answer_key"], "answer1")


class TwoPhaseContractTest(unittest.TestCase):
    """把「两段式 + 只由主 agent 提交」钉在文档和工具描述里，防止以后被改回去。"""

    def setUp(self):
        self.server = (ROOT / "server.py").read_text(encoding="utf-8")
        self.playbook = (ROOT / "docs" / "homework-playbook.md").read_text(encoding="utf-8")
        self.orchestration = (ROOT / "docs" / "main-agent-orchestration.md").read_text(encoding="utf-8")

    def test_usage_readme_exists_and_covers_setup(self):
        """别人拿到这份代码要能照着配起来 —— USAGE.md 必须在，且覆盖关键步骤。"""
        usage = (ROOT / "USAGE.md").read_text(encoding="utf-8")
        for kw in ("pip install -r requirements.txt", "codex mcp add", "XUEXITONG_USERNAME",
                   "review_token", "submit_homework", ".xuexitong_mcp", "list_prepared_submissions"):
            self.assertIn(kw, usage)
        self.assertFalse((ROOT / ".env").exists(), "项目目录里不该有真实 .env")
        self.assertFalse((ROOT / "state").exists(), "项目目录里不该有运行时 state/")

    def test_server_instructions_declare_two_phases(self):
        self.assertIn("工作流分两段", self.server)
        self.assertIn("阶段一", self.server)
        self.assertIn("子 agent 只做阶段一", self.server)

    def test_playbook_forbids_submit_tools_for_subagents(self):
        self.assertIn("submit_homework", self.playbook)
        self.assertIn("batch_submit", self.playbook)
        self.assertIn("一次都不要调用", self.playbook)
        self.assertIn("PREPARED", self.playbook.replace("READY_TO_SUBMIT", "PREPARED"))
        self.assertIn("phase", self.playbook)

    def test_orchestration_doc_covers_both_branches(self):
        for kw in ("阶段一", "阶段二", "提交", "重修", "confirm=true", "list_prepared_submissions"):
            self.assertIn(kw, self.orchestration)

    def test_one_shot_finish_plus_submit_is_forbidden(self):
        """铁律：无论用户怎么说，都不许把「完成」和「提交」一步做完。"""
        import server as srv
        self.assertIn("完成和提交永远不能一步做完", srv.SERVER_INSTRUCTIONS)
        self.assertIn("一步做完", self.orchestration)
        self.assertIn("review_token", self.orchestration)
        self.assertIn("review_token", self.playbook)
        # 提交工具必须要求 review_token（技术闸门）
        tools = {x["name"]: x for x in srv.TOOLS}
        props = tools["submit_homework"]["inputSchema"]["properties"]
        self.assertIn("review_token", props)
        self.assertIn("一步", props["review_token"]["description"])
        self.assertIn("review_tokens",
                      tools["batch_submit"]["inputSchema"]["properties"])

    def test_review_token_is_stable_fingerprint(self):
        t1 = t._review_token_for("/u", {"a": "1", "pyFlag": "3"})
        t2 = t._review_token_for("/u", {"pyFlag": "3", "a": "1"})   # 顺序无关
        t3 = t._review_token_for("/u", {"a": "2", "pyFlag": "3"})   # 内容变 → 变
        self.assertEqual(t1, t2)
        self.assertNotEqual(t1, t3)
        self.assertTrue(t1.startswith("rv-"))

    def test_submit_tools_are_described_as_main_agent_only(self):
        import server as srv
        tools = {x["name"]: x for x in srv.TOOLS}
        self.assertIn("主 agent", tools["submit_homework"]["description"])
        self.assertIn("confirm", tools["submit_homework"]["description"])
        self.assertIn("list_prepared_submissions", tools)
        self.assertNotIn("服务里没有真提交工具", srv.SERVER_INSTRUCTIONS)
        self.assertIn("子 agent", tools["batch_submit"]["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
