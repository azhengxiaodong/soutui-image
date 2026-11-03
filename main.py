"""统一入口：A～F 已逐块验收，集中启用编排。保留旧的停用说明用于核对开发记录。"""


def module_status() -> None:
    """当前只显示开发状态，避免误触发真实生图和积分消耗。"""
    # A：读取白底图，建立商品 ID 和原图的对应关系；独立验证见 run_stage_a.py。
    # from soutui_image.stage_a_inputs import scan_inputs
    # batch = scan_inputs(input_directory)

    # B：Codex 按原图分类，在已有浏览器上传并创建以商品 ID 命名的一行项目。
    # 独立验证已通过，证据见 docs/模块验收记录.md；等待 C～F 后统一启用。
    # from soutui_image.stage_b_projects import run_stage_b
    # project = run_stage_b(product, state_directory)
    # B 接口变体：复用本机会话直接上传同一原图，回读字节核对哈希，不逐次点击。
    # 已独立验证，原 B 界面模块保留；这里也暂时注释，等待统一联调。
    # from soutui_image.stage_b_web_upload import upload_product
    # upload = upload_product(product, classification, state_directory, session_path)
    # C：接口一次读取收藏，固定 9 行同款任务和每行 5 张参考图的 ID/URL，共 45 张。
    # 接口方案独立验证见 run_stage_c.py；统一调用继续停用，待 D～F 完成。
    # from soutui_image.stage_c_web_plan import prepare_references
    # reference_plan = prepare_references(product, favorites_response, state_directory)
    # D：按固定参数生成并跟踪结果；每组不足 3 张合格图时，原组最多补生 3 轮。
    # 已独立验证一组真实五张：全部成功，按槽位关联参考图；补生规则与异常停止检查通过。
    # from soutui_image.stage_d_web_generation import prepare_job, submit_job, watch_job
    # job_path, job = prepare_job(product, upload, reference_plan, row, state_directory, transport)
    # job = submit_job(job_path, transport)
    # job = watch_job(job_path, transport)
    # E：Codex 逐张对比结果与原图，只审核画面展示的服装部分。
    # 已完成五个用户样例校准及真实首组五张逐图审核，拟通过图经独立复核。
    # from soutui_image.stage_e_codex_review import review_candidate
    # review = review_candidate(product, candidate, review_directory)
    # F：将合格图保存到商品 ID 下的 9 个分组目录，保留每组参考图的对应关系。
    # 已独立验证真实两张合格图落盘、九个目录及重跑去重；等待统一编排后集中启用。
    # from soutui_image.stage_f_outputs import save_approved_reports
    # output = save_approved_reports(product, reference_plan, reports, cache_directory, state_directory)
    print("A～F 已独立验证并停用统一调用；下一步接入统一流程并进行真实闭环联调。")


if __name__ == "__main__":
    from run_pipeline import main
    raise SystemExit(main())
