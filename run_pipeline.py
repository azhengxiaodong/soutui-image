"""集中启用已独立验收的 A～F；未加 --run 只检查输入，不提交生成。"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def default_data_directory():
    """源码使用项目 data，exe 使用当前用户本机目录，不写死开发电脑用户名。"""
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "搜推生图"
    return ROOT / "data"


def main():
    """解析明确入口；默认开界面，只扫描不生成，带 --run 才真实执行，返回退出状态。"""
    parser = argparse.ArgumentParser(description="搜推生图：接口生成、Codex 逐图审核、分组保存")
    parser.add_argument("--input", type=Path, help="白底图目录")
    parser.add_argument("--product-id", action="append", help="仅处理指定商品；可重复传入")
    parser.add_argument("--rows", default="1,2,3,4,5,6,7,8,9", help="联调可缩小组号，默认全部九组")
    parser.add_argument("--data-dir", type=Path, default=default_data_directory())
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--model", help="当前账号已支持的 Codex 模型；默认 gpt-6.1-sol")
    parser.add_argument("--run", action="store_true", help="实际执行生图，使用网站积分")
    parser.add_argument("--recheck-saved", action="store_true", help="只按新版规则复核旧输出，不生成新图")
    parser.add_argument("--self-test", action="store_true", help="只检查依赖，不联网、不生成")
    parser.add_argument("--connect-existing", action="store_true", help="仅连接已有生图浏览器会话，不生成")
    parser.add_argument("--ui-smoke-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--check-updates",action="store_true",help="只检查私有规则更新，不生成或审核图片")
    parser.add_argument("--apply-program-update",type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--update-target",type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--wait-pid",type=int,default=0,help=argparse.SUPPRESS)
    parser.add_argument("--upgrade-rules",action="store_true",help="明确将旧商品规则升级到最新稳定版并复核已有结果")
    args = parser.parse_args()
    if args.check_updates:
        from soutui_image.update_service import UpdateService
        result=UpdateService(args.data_dir).check()
        if sys.stdout is not None: print(json.dumps(result,ensure_ascii=True))
        return 0 if result["status"]=="updated" else 2
    if args.apply_program_update:
        from soutui_image.program_update import apply_program_update
        import subprocess
        if args.update_target is None: raise ValueError("程序升级缺少目标")
        result=apply_program_update(args.apply_program_update,args.update_target,wait_pid=args.wait_pid,data_directory=args.data_dir)
        (args.apply_program_update.parent/"install_result.json").write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
        subprocess.Popen([str(args.update_target/"搜推生图.exe"),"--data-dir",str(args.data_dir)],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return 0
    if args.connect_existing:
        from soutui_image.browser_connection import connect_existing_session
        from soutui_image.pipeline import PipelinePaths
        from soutui_image.web_session import MusefitReadClient
        args.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            paths = PipelinePaths(args.data_dir.resolve())
            result = connect_existing_session(paths.session)
            favorites = MusefitReadClient(paths.session).get_json("/api/web/favorites", {"mediaType": "image"})
            result.update(named_groups=len(favorites.get("folders", [])), generation_started=False)
        except Exception as error:
            import traceback
            (args.data_dir / "connection_error.log").write_text(traceback.format_exc(), encoding="utf-8")
            result = {"status": "needs_attention", "error": str(error), "generation_started": False}
        (args.data_dir / "connection_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if sys.stdout is not None: print(json.dumps(result, ensure_ascii=True))
        return 0 if result["status"] == "connected" else 2
    if args.self_test:
        from soutui_image.codex_clothing import find_codex
        from PIL import Image
        import uiautomation
        import comtypes
        import tkinter
        import ssl
        import inspect
        from soutui_image.stage_e_codex_review import _audit_once, POLICY_VERSION
        from soutui_image.quality_upgrade import DEFAULT_REVIEW_MODEL
        from soutui_image.update_config import APP_VERSION,update_source
        from soutui_image.rules_bundle import builtin_bundle
        from soutui_image.update_security import verify_payload
        context = ssl.create_default_context()
        result = {"status": "ok", "python": sys.version.split()[0], "Pillow": Image.__version__,
                  "tk": tkinter.TkVersion, "uiautomation_imported": True, "comtypes_imported": True,
                  "https_context_created": True, "https_verification_required": context.verify_mode == ssl.CERT_REQUIRED,
                  "review_model": DEFAULT_REVIEW_MODEL, "policy_version": POLICY_VERSION,
                  "app_version":APP_VERSION,"private_update_source":update_source()["owner"]+"/"+update_source()["repository"],
                  "builtin_rules_sha256":builtin_bundle().sha256,"signature_support":True,
                  "audit_timeout_per_call_seconds": inspect.signature(_audit_once).parameters["timeout"].default}
        try:
            result["codex_found"] = find_codex().is_file()
        except (OSError, RuntimeError):
            result.update(status="needs_setup", codex_found=False, instruction="请在本机安装 Codex，并登录自己的账号")
        args.data_dir.mkdir(parents=True, exist_ok=True)
        (args.data_dir / "self_test_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if sys.stdout is not None: print(json.dumps(result, ensure_ascii=True))
        return 0 if result["status"] == "ok" else 2
    if args.gui or args.ui_smoke_output or not args.input:
        from soutui_image.desktop_app import launch
        launch(args.data_dir, initial_directory=args.input, smoke_output=args.ui_smoke_output)
        return 0
    from soutui_image.stage_a_inputs import scan_inputs
    if not args.run and not args.recheck_saved:
        batch = scan_inputs(args.input)
        print(json.dumps({"mode": "input_check_only", **batch.to_dict()}, ensure_ascii=True, indent=2))
        return 0 if batch.ready else 2
    from soutui_image.pipeline import Pipeline, PipelinePaths
    from soutui_image.runtime_lock import pipeline_lock
    paths = PipelinePaths(args.data_dir.resolve())
    try:
        rows = tuple(int(value.strip()) for value in args.rows.split(","))
        paths.root.mkdir(parents=True, exist_ok=True)
        events = paths.root / "last_run.events.jsonl"
        def notify(event):
            print(json.dumps(event, ensure_ascii=True), flush=True)
            with events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        with pipeline_lock(paths.root):
            runner = Pipeline(paths, on_event=notify, review_workers=args.workers, model=args.model)
            result = (runner.recheck_saved(args.input,product_ids=args.product_id,upgrade_rules=args.upgrade_rules) if args.recheck_saved else
                      runner.run(args.input, product_ids=args.product_id, rows=rows))
        (paths.root / "last_run_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if result["status"] == "complete" else 2
    except Exception as error:
        result = {"status": "needs_attention", "error": str(error), "products": []}
        paths.root.mkdir(parents=True, exist_ok=True)
        (paths.root / "last_run_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if sys.stdout is not None: print(json.dumps(result, ensure_ascii=True), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
