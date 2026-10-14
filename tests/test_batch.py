"""批量编排的离线测试（全部 mock，不碰网络）。

钉住三件事：
  · 单个作业失败不影响其它（错误隔离）
  · 批量取材料**只回摘要**，不含全文（防上下文爆炸）
  · batch_submit 没有 confirm 时**零提交**，有 confirm 才逐个带 confirm=True
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

from xuexitong import tasks  # noqa: E402


class BatchPromise(unittest.TestCase):
    def test_prepare_materials_is_compact_and_isolates_failure(self):
        big_text = "很长的全文" * 5000

        def fake_materials(*, work_id, **kw):
            if work_id == "2":
                raise RuntimeError("这个作业解析炸了")
            return {"status": "OK", "homework_name": f"作业{work_id}",
                    "folder": f"D:/x/{work_id}", "files": [{"rel": "a.docx"}],
                    "documents": [{"file": "a.docx", "text": big_text, "paragraph_count": 10}],
                    "template_suggestion": {"file": "a.docx"},
                    "personal_fields": ["姓名"], "warnings": [], "materials_path": f"p/{work_id}.json"}

        with mock.patch.object(tasks, "get_homework_materials", side_effect=fake_materials):
            out = tasks.batch_prepare_materials(work_ids=["1", "2", "3"])

        self.assertEqual((out["count"], out["ok"], out["failed"]), (3, 2, 1))
        by_id = {r["work_id"]: r for r in out["rows"]}
        self.assertEqual(by_id["2"]["status"], "ERROR")
        self.assertIn("解析炸了", by_id["2"]["error"])
        self.assertEqual(by_id["1"]["documents"], ["a.docx"])
        self.assertEqual(by_id["1"]["personal_fields"], ["姓名"])
        # 关键：不能把全文带回上下文
        self.assertNotIn("text", json.dumps(out, ensure_ascii=False)[:20000])
        self.assertNotIn("很长的全文", json.dumps(out, ensure_ascii=False)[:20000])
        self.assertIn("勿把全文塞回上下文", out["summary"])

    def test_apply_edits_passes_items_and_aggregates(self):
        calls = []

        def fake_apply(**kw):
            calls.append(kw)
            if kw["work_id"] == "9":
                raise ValueError("锚点全没命中")
            return {"output": f"D:/out/{kw['work_id']}.docx", "file_size": 100,
                    "applied_count": 2, "not_found": [], "refused_personal": [],
                    "needs_user": [{"fields": ["学号"]}], "insufficient": []}

        items = [{"work_id": "1", "edits": [{"anchor": "A", "content": "x"}]},
                 {"work_id": "9", "edits": [{"anchor": "B", "content": "y"}]}]
        with mock.patch.object(tasks, "apply_homework_edits", side_effect=fake_apply):
            out = tasks.batch_apply_edits(items=items)

        self.assertEqual(len(calls), 2)
        self.assertEqual(out["ok"], 1)
        self.assertEqual(out["rows"][1]["status"], "ERROR")
        self.assertEqual(out["rows"][0]["needs_user"], [{"fields": ["学号"]}])

    def test_prepare_submissions_writes_payload_files(self):
        tmp = Path(tempfile.mkdtemp())

        def fake_prepare(**kw):
            return {"post_url": "/mooc-ans/work/addStudentWorkNewWeb?token=T",
                    "method": "post", "fields_full": {"pyFlag": "3", "answer1": "<p>x</p>"},
                    "mode": "提交(pyFlag=3)", "note": "n", "answer_keys": ["answer1"],
                    "field_count": 2, "uploaded": [{"objectid": "O1"}], "summary": "s"}

        with mock.patch.object(tasks, "prepare_homework_submission", side_effect=fake_prepare):
            out = tasks.batch_prepare_submissions(work_ids=["11", "12"], payload_dir=str(tmp))

        self.assertEqual(out["ok"], 2)
        p = tmp / "11.payload.json"
        self.assertTrue(p.is_file())
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["fields"]["answer1"], "<p>x</p>")
        self.assertEqual(data["fields"]["pyFlag"], "3")

    def test_batch_submit_without_confirm_sends_nothing(self):
        seen = []

        def fake_submit(**kw):
            seen.append(kw)
            return {"submitted": False, "post_url": "/u", "answer_keys": ["answer1"],
                    "summary": "未提交"}

        with mock.patch.object(tasks, "submit_homework", side_effect=fake_submit):
            out = tasks.batch_submit(work_ids=["1", "2"], confirm=False)

        self.assertEqual(len(seen), 2)
        self.assertTrue(all(k["confirm"] is False for k in seen))
        self.assertTrue(all(r["submitted"] is False for r in out["rows"]))
        self.assertIn("未发送任何提交请求", out["summary"])

    def test_batch_submit_with_confirm_propagates_true(self):
        seen = []

        def fake_submit(**kw):
            seen.append(kw)
            return {"submitted": True, "post_url": "/u", "answer_keys": ["answer1"],
                    "response": {"status": True}, "summary": "已提交"}

        with mock.patch.object(tasks, "submit_homework", side_effect=fake_submit):
            out = tasks.batch_submit(work_ids=["1", "2", "3"], confirm=True)

        self.assertTrue(all(k["confirm"] is True for k in seen))
        self.assertEqual(sum(1 for r in out["rows"] if r["submitted"]), 3)
        self.assertIn("成功提交 3 个", out["summary"])

    def test_batch_submit_passes_each_works_review_token(self):
        """每个作业必须带自己的 review_token（=各自展示给用户过）。"""
        seen = []

        def fake_submit(**kw):
            seen.append(kw)
            return {"submitted": True, "summary": "已提交"}

        with mock.patch.object(tasks, "submit_homework", side_effect=fake_submit):
            tasks.batch_submit(work_ids=["1", "2"], confirm=True,
                               review_tokens={"1": "rv-aaa", "2": "rv-bbb"})

        self.assertEqual({k["work_id"]: k["review_token"] for k in seen},
                         {"1": "rv-aaa", "2": "rv-bbb"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
