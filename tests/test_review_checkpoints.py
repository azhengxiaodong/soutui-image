"""模型中断只重做未完成阶段；测试模型使用模拟输出，真实续跑另有实证。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from soutui_image.review_checkpoints import save_checkpoint,load_checkpoint
from soutui_image.review_detail_views import prepare_detail_views
from soutui_image.review_images import inspect_image
from soutui_image.stage_a_inputs import scan_inputs
from soutui_image.stage_e_codex_review import _audit_once, _prompt, REVIEW_SCHEMA
from test_stage_e_review import response


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_model_and_input_change_cannot_reuse_phase(self):
        raw=self.root/"response.json"; raw.write_text('{"decision":"pass"}',encoding="utf-8")
        path=self.root/"checkpoint.json"
        save_checkpoint(path,{"model":"sol","image":"one"},{"decision":"pass"},response=raw)
        with self.assertRaises(ValueError): load_checkpoint(path,{"model":"astra","image":"one"})
        raw.write_text('{"decision":"reject"}',encoding="utf-8")
        with self.assertRaises(ValueError): load_checkpoint(path,{"model":"sol","image":"one"})

    def test_checkpoint_cannot_override_unchanged_raw_response(self):
        raw=self.root/"response.json"; raw.write_text('{"decision":"review"}',encoding="utf-8")
        path=self.root/"checkpoint.json"; save_checkpoint(path,{"x":1},{"decision":"review"},response=raw)
        data=json.loads(path.read_text(encoding="utf-8")); data["verdict"]["decision"]="pass"
        path.write_text(json.dumps(data),encoding="utf-8")
        with self.assertRaises(ValueError): load_checkpoint(path,{"x":1})

    def setup_images(self):
        source=self.root/"001.png"; Image.new("RGB",(80,100),"white").save(source)
        product=scan_inputs(self.root).products[0]
        candidate=inspect_image("001","task/slot-0",source)
        regions=[{"role":r,"part":"门襟与扣列","visible":True,"box":[300,100,700,900]} for r in ("original","generated")]
        return product,candidate,regions

    def critical(self,product,candidate,views,directory,**kwargs):
        directory.mkdir()
        check=copy.deepcopy(next(c for c in response()["checks"] if c["feature"]=="扣位边缘与凸片"))
        (directory/"response.json").write_text(json.dumps(check),encoding="utf-8")
        return check

    def test_failed_critical_resume_does_not_repeat_successful_body(self):
        product,candidate,regions=self.setup_images()
        attempt=self.root/"run/verification"
        def model(command,**kwargs):
            target=Path(command[command.index("--output-last-message")+1]); target.write_text(json.dumps(response()),encoding="utf-8")
            return SimpleNamespace(returncode=0,stdout="",stderr="")
        with patch("soutui_image.stage_e_codex_review.find_codex",return_value=Path("codex.exe")), \
             patch("soutui_image.stage_e_codex_review.locate_detail_regions",return_value=regions), \
             patch("soutui_image.stage_e_codex_review.subprocess.run",side_effect=model) as calls, \
             patch("soutui_image.stage_e_codex_review.review_button_edges",side_effect=TimeoutError("中断")):
            with self.assertRaises(TimeoutError): _audit_once(product,candidate,attempt,verification=True)
            self.assertEqual(calls.call_count,1)
        with patch("soutui_image.stage_e_codex_review.find_codex",return_value=Path("codex.exe")), \
             patch("soutui_image.stage_e_codex_review.locate_detail_regions",return_value=regions), \
             patch("soutui_image.stage_e_codex_review.subprocess.run",side_effect=AssertionError("不得重复整图审核")), \
             patch("soutui_image.stage_e_codex_review.review_button_edges",side_effect=self.critical):
            self.assertEqual(_audit_once(product,candidate,attempt,verification=True)["decision"],"pass")

    def test_legacy_validated_body_recovers_from_real_log_and_next_phase(self):
        product,candidate,regions=self.setup_images()
        attempt=self.root/"run/verification"; attempt.mkdir(parents=True)
        views=prepare_detail_views(product,candidate,attempt/"detail_views",regions=regions)
        (attempt/"schema.json").write_text(json.dumps(REVIEW_SCHEMA),encoding="utf-8")
        (attempt/"response.json").write_text(json.dumps(response()),encoding="utf-8")
        prompt=_prompt(product,candidate,verification=True,views=views)
        (attempt/"stderr.txt").write_text('model: gpt-6.1-sol\n'+prompt,encoding="utf-8")
        (attempt/"button_focus").mkdir()
        with patch("soutui_image.stage_e_codex_review.find_codex",return_value=Path("codex.exe")), \
             patch("soutui_image.stage_e_codex_review.locate_detail_regions",return_value=regions), \
             patch("soutui_image.stage_e_codex_review.subprocess.run",side_effect=AssertionError("不得重复整图审核")), \
             patch("soutui_image.stage_e_codex_review.review_button_edges",side_effect=self.critical):
            self.assertEqual(_audit_once(product,candidate,attempt,verification=True)["decision"],"pass")
        self.assertTrue((attempt/"body_checkpoint.json").exists())
