"""统一编排暂停和多开保护，不能因启动两个窗口而重复提交同一批任务。"""

import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from soutui_image.pipeline import Pipeline, PipelinePaths
from soutui_image.runtime_lock import pipeline_lock


@dataclass
class Candidate:
    result_id: str = "task/slot-0"


@dataclass
class Review:
    decision: str = "reject"
    summary: str = "实际差异，拒绝"


class PipelineControlTests(unittest.TestCase):
    def test_paused_run_never_opens_network_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "inputs"; source.mkdir()
            Image.new("RGB", (10, 10), "white").save(source / "001.jpg")
            stop = threading.Event(); stop.set()
            with patch("soutui_image.pipeline.WebTransport", side_effect=AssertionError("must not create network client")):
                result = Pipeline(PipelinePaths(root / "state"), stop_event=stop).run(source)
            self.assertEqual(result["status"], "paused")

    def test_only_one_pipeline_can_hold_same_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with pipeline_lock(directory):
                with self.assertRaises(RuntimeError):
                    with pipeline_lock(directory): pass
            with pipeline_lock(directory): pass

    def test_invalid_rows_stop_before_input_scan(self):
        with patch("soutui_image.pipeline.scan_inputs", side_effect=AssertionError("must not scan")):
            for rows in ((), (1, 1), (0,), (10,)):
                with self.subTest(rows=rows), self.assertRaises(ValueError):
                    Pipeline(PipelinePaths(Path("unused"))).run("unused", rows=rows)

    def _job(self):
        return {"row": 1, "round_index": 0, "task_id": "task", "folder_id": "folder", "reference_ids": ["ref"],
                "failed_results": 0, "results": [{"status": "done", "reference_id": "ref", "reference_index": 0}]}

    def test_one_timeout_retries_review_without_generating(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            pipeline = Pipeline(PipelinePaths(Path(directory)), on_event=events.append)
            product = SimpleNamespace(product_id="001", sha256="source")
            with patch("soutui_image.pipeline.download_result", return_value=Candidate()), \
                 patch("soutui_image.pipeline.review_candidate", side_effect=[TimeoutError(), Review()]) as review, \
                 patch("soutui_image.pipeline.write_group_review_summary", return_value={"verified": True}), \
                 patch("soutui_image.pipeline.submit_job", side_effect=AssertionError("不能因审核超时生成新图")):
                self.assertEqual(pipeline._review(product, self._job()), {"verified": True})
            self.assertEqual(review.call_count, 2)
            self.assertEqual(sum(e["kind"] == "review_retry" for e in events), 1)

    def test_second_timeout_stops_without_complete_review_report(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = Pipeline(PipelinePaths(Path(directory)))
            with patch("soutui_image.pipeline.download_result", return_value=Candidate()), \
                 patch("soutui_image.pipeline.review_candidate", side_effect=TimeoutError()) as review, \
                 patch("soutui_image.pipeline.write_group_review_summary") as summary:
                with self.assertRaises(TimeoutError):
                    pipeline._review(SimpleNamespace(product_id="001", sha256="source"), self._job())
            self.assertEqual(review.call_count, 2)
            summary.assert_not_called()
            self.assertFalse(list(Path(directory).rglob("round_review.json")))


if __name__ == "__main__":
    unittest.main()
