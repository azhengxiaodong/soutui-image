"""旧计数升级必须有新证据；补生轮次不重置，已到三轮时仍暂停。"""

import copy
import json
import unittest

import test_review_group_state as fixtures
from soutui_image.review_group_state import write_group_review_summary
from soutui_image.stage_c_web_plan import GroupAssignment, ReferenceImage
from soutui_image.stage_d_web_generation import group_decision
from soutui_image.stage_e_codex_review import POLICY_VERSION


class QualitySummaryTests(unittest.TestCase):
    setUp=fixtures.GroupReviewTests.setUp
    fixture=fixtures.GroupReviewTests.fixture

    def old_summary(self):
        result=write_group_review_summary(self.product,self.state,self.cache,1)
        result.pop("policy_version")
        path=self.jobs/"review_summary.json"
        path.write_text(json.dumps(result),encoding="utf-8")
        return path

    def reject(self,path,report):
        verdict=copy.deepcopy(report["outcomes"][0]["review"]["passes"][0])
        verdict["decision"]="reject"; verdict["checks"][0]["comparison"]="mismatch"
        report["outcomes"][0]["review"].update(decision="reject",passes=[verdict])
        path.write_text(json.dumps(report),encoding="utf-8")

    def test_old_count_cannot_be_changed_without_explicit_revision(self):
        path,report=self.fixture(0); summary=self.old_summary(); old=summary.read_bytes()
        self.reject(path,report)
        with self.assertRaises(ValueError): write_group_review_summary(self.product,self.state,self.cache,1)
        self.assertEqual(summary.read_bytes(),old)

    def test_new_rejection_can_revoke_old_pass_with_history_and_same_rounds(self):
        path,report=self.fixture(0); summary=self.old_summary(); old=summary.read_bytes()
        job=self.jobs/"round_00.json"; before=job.read_bytes()
        self.reject(path,report)
        result=write_group_review_summary(self.product,self.state,self.cache,1,allow_policy_revision=True)
        self.assertEqual(result["approved_count"],0); self.assertEqual(result["reviewed_rounds"],[0])
        self.assertEqual(job.read_bytes(),before)
        self.assertEqual(next((self.jobs/"review_history").glob("*.json")).read_bytes(),old)

    def test_missing_new_evidence_cannot_erase_an_old_approval(self):
        self.fixture(0); summary=self.old_summary()
        data=json.loads(summary.read_text(encoding="utf-8")); data["approved_sha256"].append("c"*64)
        summary.write_text(json.dumps(data),encoding="utf-8")
        with self.assertRaises(ValueError):
            write_group_review_summary(self.product,self.state,self.cache,1,allow_policy_revision=True)

    def test_legacy_count_never_marks_complete_and_three_rounds_not_reset(self):
        for i in range(4):
            self.fixture(i)
            path=self.jobs/f"round_{i:02}.json"; data=json.loads(path.read_text(encoding="utf-8"))
            data["state"]="awaiting_audit"; path.write_text(json.dumps(data),encoding="utf-8")
        summary=self.old_summary()
        group=GroupAssignment(1,"17","组17",tuple(ReferenceImage(str(i),f"https://example.test/{i}.jpg") for i in range(5)))
        self.assertEqual(group_decision(self.state,self.product,group).action,"audit")
        data=json.loads(summary.read_text(encoding="utf-8")); data.update(policy_version=POLICY_VERSION,approved_sha256=[],approved_count=0)
        summary.write_text(json.dumps(data),encoding="utf-8")
        self.assertEqual(group_decision(self.state,self.product,group).action,"paused")
        self.assertEqual(len(list(self.jobs.glob("round_*.json"))),4)
