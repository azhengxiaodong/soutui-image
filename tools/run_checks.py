"""统一执行现有检查并保留实际结果；显式加入 src，避免依赖本机可编辑安装。"""

import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


if __name__ == "__main__":
    target = ROOT / "validation" / "integration"
    target.mkdir(parents=True, exist_ok=True)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT / "tests")))
    (target / "code_checks.log").write_text(output.getvalue(), encoding="utf-8")
    report = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "success": result.wasSuccessful(),
              "scope": "现有模块规则和编排控制检查；真实网站与图片审核另见联调证据"}
    (target / "code_checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    raise SystemExit(0 if result.wasSuccessful() else 1)
