"""已完成单图可进入审核后，后续网站快照不得偷换其身份或完成状态。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from soutui_image.stage_d_web_generation import _apply_task


class StreamIdentityTests(unittest.TestCase):
    def snapshot(self):
        params={'resolution':'2K','aspectRatio':'3:4','inputImageUrls':['https://example.test/source'],
                'inputImageUrls2':[f'https://example.test/ref{i}' for i in range(5)],
                'extraParams':{'imageTier':'balanced','garmentType':'套装','resultsPerReference':'1','selectedPoseCount':'5'}}
        record={'task_id':'task','reference_ids':[str(i) for i in range(5)],'payload':{'params':params}}
        task={'id':'task','featureType':'model-dressing','expectedResultCount':5,'status':'running',**params,
              'resultSlots':[{'id':f'slot-{i}','referenceIndex':i,'status':'done' if i==0 else 'pending',
                              'imageUrl':'https://example.test/result0' if i==0 else None} for i in range(5)]}
        return record,task

    def test_partial_completion_is_exposed_while_group_keeps_generating(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'job.json';record,task=self.snapshot()
            result=_apply_task(path,record,task)
            self.assertEqual(result['state'],'generating')
            self.assertEqual(result['successful_results'],1)
            self.assertEqual(result['results'][0]['status'],'done')

    def test_later_snapshot_cannot_replace_a_ready_image_or_slot(self):
        for change in ('url','state','slot'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'job.json';record,task=self.snapshot()
                record=_apply_task(path,record,task);before=path.read_bytes();altered=copy.deepcopy(task)
                if change=='url':altered['resultSlots'][0]['imageUrl']='https://example.test/other'
                if change=='state':altered['resultSlots'][0]['status']='pending'
                if change=='slot':altered['resultSlots'][1]['id']='another-slot'
                with self.assertRaises(ValueError):_apply_task(path,record,altered)
                self.assertEqual(path.read_bytes(),before)

    def test_remaining_slots_can_finish_without_changing_first_image(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'job.json';record,task=self.snapshot()
            record=_apply_task(path,record,task);task['status']='done'
            for i,slot in enumerate(task['resultSlots']):
                slot.update(status='done',imageUrl=f'https://example.test/result{i}')
            result=_apply_task(path,record,task)
            self.assertEqual(result['state'],'awaiting_audit')
            self.assertEqual(result['successful_results'],5)
