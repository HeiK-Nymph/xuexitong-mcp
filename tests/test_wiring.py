# -*- coding: utf-8 -*-
"""接线回归测试：MCP 每个工具的 handler 传给 tasks.* 的关键字参数，必须真的存在于该函数签名里。

来历：实测发现 server.py 给 upload/prepare/submit_homework 传了 root=…，
但这三个 tasks 函数当时没有 root 形参 → 一调用就是 TypeError「内部错误」。
这个测试把「schema ↔ handler ↔ tasks 签名」三者的接线钉死，避免同类问题再溜过去。
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server as srv  # noqa: E402
from xuexitong import tasks  # noqa: E402


def _sample(schema: dict, name: str):
    t = (schema.get(name) or {}).get("type")
    if t == "boolean":
        return True
    if t == "integer":
        return 1
    if t == "array":
        return ["1"]
    if t == "object":
        return {"1": "2"}
    return "1"


class HandlerWiringTest(unittest.TestCase):
    def test_every_handler_passes_kwargs_the_task_function_accepts(self):
        problems: list[str] = []
        originals = {a: getattr(tasks, a) for a in dir(tasks)
                     if inspect.isfunction(getattr(tasks, a)) and not a.startswith("_")}

        def stub_for(orig):
            sig = inspect.signature(orig)

            def stub(*args, **kwargs):
                try:
                    sig.bind(*args, **kwargs)
                except TypeError as exc:
                    problems.append(f"{orig.__name__}: {exc}")
                    raise
                return {"summary": "stub", "ok": True}

            return stub

        with mock.patch.multiple(tasks, **{a: stub_for(f) for a, f in originals.items()}):
            for tool in srv.TOOLS:
                props = tool["inputSchema"].get("properties") or {}
                args = {name: _sample(props, name) for name in props}
                try:
                    srv.call_tool(tool["name"], args)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"工具 {tool['name']}: {type(exc).__name__}: {exc}")

        self.assertEqual(problems, [], "handler ↔ tasks 签名不一致：\n" + "\n".join(problems))

    def test_submit_handler_requires_review_token_before_any_request(self):
        """确认闸门在**任何请求之前**：confirm=true 且无 token → 抛错，且没有网络调用。"""
        with mock.patch.object(tasks, "get_client",
                               side_effect=AssertionError("不该联网")):
            with self.assertRaises(tasks.TaskError) as ctx:
                srv.call_tool("submit_homework", {"work_id": "1", "confirm": True})
        self.assertIn("先展示、后提交", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
