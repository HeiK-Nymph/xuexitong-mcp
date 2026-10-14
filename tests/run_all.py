"""一次跑完所有测试：python tests/run_all.py"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

MODULES = ["test_crypto", "test_session", "test_auth_flow", "test_homework_parse", "test_account_switch", "test_materials_edits", "test_prompt_attachments", "test_submit", "test_batch", "test_phase", "test_wiring", "test_worklist"]


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in MODULES:
        suite.addTests(loader.loadTestsFromName(name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    print()
    print("=" * 60)
    print(f"用例总数 {total}  失败 {len(result.failures)}  错误 {len(result.errors)}  "
          f"跳过 {len(result.skipped)}")
    print("=" * 60)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
