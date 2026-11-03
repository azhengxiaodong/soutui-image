"""用35份真实旧图复核证据验证规则外置逐字一致；不调用模型、不改变图片或旧模块。"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))
from soutui_image.rules_bundle import builtin_bundle
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.review_images import ReviewImage
from soutui_image.stage_e_codex_review import _prompt, FEATURES
from soutui_image.review_checkpoints import fingerprint
from tools.check_frozen import check_frozen


def read(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    bundle = builtin_bundle()
    products = {p.product_id: p for p in scan_inputs(r"C:\Users\王\Desktop\家居服白底").products}
    cases = []; body_count = locator_count = button_count = 0
    for report_path in (ROOT / "data/review_cache").glob("*/group_*/round_*/round_review.json"):
        report = read(report_path); product = products.get(report["product_id"])
        if product is None: continue
        for outcome in report["outcomes"]:
            review = outcome["review"]
            if review["policy_version"] != bundle.version: continue
            candidate = ReviewImage(**outcome["candidate"])
            assert sha(product.source_path) == product.sha256 and sha(candidate.local_path) == candidate.sha256
            evidence = Path(review["evidence_directory"])
            counts = 0
            for verification, stage in ((False, "first"), (True, "verification")):
                checkpoint = evidence / stage / "body_checkpoint.json"
                response = evidence / stage / "response.json"
                if not checkpoint.exists() and not response.exists(): continue
                views = read(evidence / stage / "detail_views/manifest.json")["views"]
                attachments = "\n".join(f"附件{i}: {'原图' if v['role']=='original' else '生成图'}的{v['region']}，原像素框={v['box_pixels']}" for i,v in enumerate(views,3))
                text = bundle.render("body", product_id=product.product_id, result_id=candidate.result_id,
                    review_kind="独立复核" if verification else "首次审核", attachments=attachments,
                    features_json=json.dumps(FEATURES,ensure_ascii=False))
                assert text == _prompt(product,candidate,verification=verification,views=views)
                if checkpoint.exists():
                    assert fingerprint(text) == read(checkpoint)["identity"]["prompt"]
                else:
                    # 阶段断点功能之前的真实记录，以成功响应和原始调用日志交叉验证，不补造断点。
                    assert text in (evidence / stage / "stderr.txt").read_text(encoding="utf-8")
                    assert read(response)["product_id"] == product.product_id
                    assert read(response)["result_id"] == candidate.result_id
                body_count += 1; counts += 1
            assert counts >= 1, "缺少真实已完成整图证据"
            locator = evidence / "detail_location/stderr.txt"
            if locator.exists():
                assert bundle.render("locator") in locator.read_text(encoding="utf-8")
                locator_count += 1
            for log in evidence.glob("verification/button_focus*/stderr.txt"):
                segments = log.parent / "segments/manifest.json"
                if not segments.exists() or not (log.parent/"response.json").exists(): continue
                values=read(segments)
                attachments="\n".join(f"附件{i}: {'原图' if s['role']=='original' else '生成图'}门襟第{s['part']}段" for i,s in enumerate(values,3))
                assert bundle.render("buttons",attachments=attachments) in log.read_text(encoding="utf-8")
                button_count += 1
            cases.append({"product_id":product.product_id,"result_id":candidate.result_id,"source_sha256":product.sha256,
                          "result_sha256":candidate.sha256,"completed_body_stages":counts,"existing_decision":review["decision"]})
    assert len(cases)==35 and not check_frozen(ROOT)
    files=["src/soutui_image/rules_bundle.py","src/soutui_image/resources/review_rules.json","tools/export_review_rules.py",
           "tools/validate_rule_bundle.py","tests/test_rules_bundle.py"]
    target=ROOT/"validation/updates/U_RULES"; target.mkdir(parents=True,exist_ok=False)
    result={"status":"verified","scope":"无损外置规则：35张实际原图/结果、已完成整图提示词哈希及可用定位/扣位日志逐字一致；未新调用模型",
            "bundle_sha256":bundle.sha256,"policy_version":bundle.version,"cases":cases,
            "body_stages":body_count,"locator_logs":locator_count,"button_logs":button_count,
            "cloud_calls":0,"old_modules_unchanged":True,"files":{f:sha(ROOT/f) for f in files}}
    (target/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    manifest=ROOT/"validation/frozen_modules.json"; original=manifest.read_bytes()
    data=json.loads(original); assert "U_RULES" not in data["modules"]
    data["modules"]["U_RULES"]={"status":"verified","scope":result["scope"],"evidence":["validation/updates/U_RULES/result.json"],"files":result["files"]}
    (target/"frozen_before.json").write_bytes(original)
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with (ROOT/"docs/模块验收记录.md").open("a",encoding="utf-8") as handle:
        handle.write("\n\n## U_RULES规则包独立验收\n\n输入为现有正式规则与35份实际图像审核证据，输出可加载的只读数据模板。执行`python tools/validate_rule_bundle.py`；证据`validation/updates/U_RULES/result.json`。整图提示词与实际阶段指纹一致，定位/扣位日志逐字一致；没有新模型调用，不声称产生新的视觉识别效果。统一入口尚未接入，本模块独立冻结。\n\n| 文件 | 实际SHA-256 |\n| --- | --- |\n")
        for f,v in result["files"].items(): handle.write(f"| `{f}` | `{v}` |\n")
    print(json.dumps({k:v for k,v in result.items() if k not in ("files","cases")},ensure_ascii=True))


if __name__=="__main__": main()
