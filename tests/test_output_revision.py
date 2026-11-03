"""旧输出撤回的字节保护、未知文件保护和两种崩溃恢复；不测试模型准确性。"""

import json
import unittest
from unittest.mock import patch

from soutui_image.stage_f_output_revision import withdraw_legacy_outputs
from soutui_image.stage_f_outputs import MANIFEST_NAME
import test_stage_f_outputs as fixtures


class OutputRevisionTests(unittest.TestCase):
    setUp = fixtures.OutputTests.setUp
    report = fixtures.OutputTests.report
    save = fixtures.OutputTests.save

    def old_output(self):
        self.save([self.report(decisions=("pass",)*5)])
        path=self.output/MANIFEST_NAME
        data=json.loads(path.read_text(encoding="utf-8"))
        for g in data["groups"]:
            for item in g["saved"]: item["policy_version"]="2026-10-27.1"
        path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        return {p:p.read_bytes() for p in (self.output/"组01").glob("*.png")}

    def test_withdraw_preserves_all_bytes_and_unmanaged_material(self):
        before=self.old_output()
        unmanaged=self.output/"组01/用户笔记.txt"; unmanaged.write_text("保留",encoding="utf-8")
        result=withdraw_legacy_outputs(self.product,self.plan,self.root/"holding")
        self.assertEqual(result["withdrawn"],5)
        record=json.loads((self.output/MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(sum(len(g["saved"]) for g in record["groups"]),0)
        from pathlib import Path
        self.assertTrue(all(Path(x["retained_path"]).read_bytes()==before[self.output/"组01"/x["filename"]] for x in record["withdrawn"]))
        self.assertTrue(unmanaged.exists())
        self.assertEqual(withdraw_legacy_outputs(self.product,self.plan,self.root/"holding")["withdrawn"],0)

    def test_resume_after_files_moved_before_manifest_written(self):
        self.old_output()
        with patch("soutui_image.stage_f_output_revision._save_manifest",side_effect=RuntimeError("模拟断电")):
            with self.assertRaises(RuntimeError): withdraw_legacy_outputs(self.product,self.plan,self.root/"holding")
        result=withdraw_legacy_outputs(self.product,self.plan,self.root/"holding")
        self.assertEqual(result["pending"],5)

    def test_resume_after_manifest_written_before_journal_completed(self):
        self.old_output()
        from soutui_image.stage_f_output_revision import _write
        count=0
        def interrupted(path,data):
            nonlocal count
            count+=1
            if count==2: raise RuntimeError("模拟断电")
            return _write(path,data)
        with patch("soutui_image.stage_f_output_revision._write",side_effect=interrupted):
            with self.assertRaises(RuntimeError): withdraw_legacy_outputs(self.product,self.plan,self.root/"holding")
        self.assertEqual(withdraw_legacy_outputs(self.product,self.plan,self.root/"holding")["pending"],5)

    def test_changed_old_image_stops_before_any_move(self):
        before=self.old_output()
        next(iter(before)).write_bytes("用户改动".encode("utf-8"))
        with self.assertRaises(ValueError): withdraw_legacy_outputs(self.product,self.plan,self.root/"holding")
        self.assertTrue(all(p.exists() for p in before))
