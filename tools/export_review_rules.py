"""U_RULES独立导出：仅从当前Python AST提取文字模板，不改变原模块或调用模型。"""

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from soutui_image.rules_bundle import RuleBundle, FEATURES


def extract(path, function, substitutions):
    """将已知f字符串表达式映射到白名单占位符；遇到未知表达式立即停止。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == function)
    value = next((x.value for x in node.body if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "prompt" for t in x.targets)), None)
    if value is None: value = next(x.value for x in node.body if isinstance(x, ast.Return))
    if isinstance(value, ast.Constant): return value.value.replace("{", "{{").replace("}", "}}")
    parts = []
    for item in value.values:
        if isinstance(item, ast.Constant): parts.append(item.value.replace("{", "{{").replace("}", "}}"))
        else:
            expression = ast.unparse(item.value)
            if expression not in substitutions or item.conversion != -1 or item.format_spec:
                raise ValueError("导出遇到未登记表达式：" + expression)
            parts.append("{" + substitutions[expression] + "}")
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = ROOT / "src/soutui_image"
    data = {"format": 1, "engine": 1, "version": "2026-10-29.5", "sequence": 1, "model": "gpt-6.1-sol",
            "timeout": 600, "flow": "double_review_button", "features": list(FEATURES), "prompts": {
        "body": extract(source / "stage_e_codex_review.py", "_prompt", {
            "product.product_id": "product_id", "candidate.result_id": "result_id", "'独立复核' if verification else '首次审核'": "review_kind",
            "attachments": "attachments", "json.dumps(FEATURES, ensure_ascii=False)": "features_json"}),
        "locator": extract(source / "review_detail_locator.py", "locate_detail_regions", {}),
        "buttons": extract(source / "review_button_edges.py", "review_button_edges", {"attachments": "attachments"})}}
    bundle = RuleBundle.parse(json.dumps(data, ensure_ascii=False).encode())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as handle: handle.write(bundle.raw)
    print(json.dumps({"version": bundle.version, "sha256": bundle.sha256, "cloud_calls": 0}, ensure_ascii=True))


if __name__ == "__main__": main()
