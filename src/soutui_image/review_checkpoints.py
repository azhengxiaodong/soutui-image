"""E 阶段断点：只有输入、模型、提示词及实际响应均可核验时，才复用已完成审核阶段。"""

import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def save_checkpoint(path, identity, verdict, *, response=None):
    """保存真正成功的阶段响应及其原始响应哈希；不能用中断或空输出建立通过证据。"""
    record={"identity":identity,"verdict":verdict}
    if response is not None:
        record.update(response_path=str(Path(response).resolve()),response_sha256=digest(response))
    temporary=Path(path).with_suffix(".tmp")
    temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    temporary.replace(path)


def load_checkpoint(path, identity):
    """阶段输入不符或原始响应被修改时停止，不能把其他图片/模型的通过带进来。"""
    record=json.loads(Path(path).read_text(encoding="utf-8"))
    if record.get("identity")!=identity:
        raise ValueError("审核阶段断点与当前模型、图片或提示词不符")
    if "response_path" in record:
        if digest(record["response_path"])!=record["response_sha256"]:
            raise ValueError("审核阶段原始响应已改变，未复用")
        if json.loads(Path(record["response_path"]).read_text(encoding="utf-8"))!=record["verdict"]:
            raise ValueError("审核阶段断点结论与原始响应不一致")
    return record["verdict"]
