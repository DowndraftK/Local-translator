"""B04 controlled invariants; these checks do not claim native OCR accuracy."""
import json
import sys
import threading
import uuid
from copy import deepcopy
from pathlib import Path
import unittest
import test_document_core as fixtures
from translator_core import CoreError, Cancelled
from translator_core.contracts import decode_snapshot, encode_snapshot, foundation_json, MAX_INPUT_BYTES
from translator_core.ocr_contracts import validate_raw


class OCRFacts:
    identity='controlled-ocr-v1'
    def __init__(self):self.calls=[];self.bad=None;self.cancel=None
    def profile(self,direction):return dict(identity=self.identity,languages=['controlled'],direction=direction)
    def require(self,direction):pass
    def recognize(self,source,fingerprint,page,direction,cancel):
        self.calls.append(page)
        text=f'Number {page}: do not remove. Repeat. Repeat.'
        raw=dict(backend=self.identity,recognitionComplete=True,observations=[dict(id=f'page{page}-word0',order=0,text=text,confidence=None,rect=[1,2,30,10])],
            pixelWidth=600,pixelHeight=800,rotation=0,cropBox=[0,0,200,267],scale=3,orientation='up',coordinateSystem='pixel-top-left',languages=['controlled'],textAngle=None)
        if self.bad:self.bad(raw)
        if self.cancel:cancel.set()
        return dict(page=page,text=text,ocr=raw)


class OCRCase(unittest.TestCase):
    # Inherit fixture setup, not B03 tests (loaded below as a separate suite).
    def setUp(self):
        fixtures.DocumentCase.setUp(self);self.ocr=OCRFacts();self.core.ocr_adapter=self.ocr
    def document(self,first=1,last=3):
        selection=self.core.select_document(self.source,'select',threading.Event())
        self.core.create_document(selection['token'],first,last,'en-zh',mode='ocr')
        return self.core.task
    def extract(self,changed=lambda _:None,cancel=None):
        return self.core.extract('ocr-op',cancel or threading.Event(),changed,mode='ocr')
    def page(self,n=1):return self.core.task['document']['pages'][n-1]

    def test_schema2_roundtrip_raw_draft_and_review_are_distinct(self):
        task=self.document();self.extract();p=self.page();raw=foundation_json(p['ocr']);revision=p['revision']
        self.core.draft(1,revision,'DRAFT-ONLY-改正')
        with self.assertRaises(CoreError) as e:self.core.run('translate',threading.Event(),lambda _:None)
        self.assertEqual(e.exception.code,'unapplied_draft');self.assertFalse(self.calls)
        saved=decode_snapshot(encode_snapshot(task));self.assertEqual(json.loads(encode_snapshot(task))['schema'],2)
        self.core.open(task['id']);self.assertEqual(self.core.task['drafts']['1']['text'],'DRAFT-ONLY-改正')
        self.assertNotIn('DRAFT-ONLY-改正',self.core.export());self.assertEqual(raw,foundation_json(self.page()['ocr']))
        self.core.apply_draft(1,revision);p=self.page();self.assertNotEqual(p['revision'],revision)
        self.assertFalse(p['reviewed']);self.assertEqual(p['effectiveSource']['kind'],'manual-correction')
        self.core.review(1,p['revision'],True);self.core.open(task['id'])
        self.assertTrue(self.page()['reviewed']);self.assertEqual(raw,foundation_json(self.page()['ocr']))

    def test_late_cancelled_page_is_not_committed_and_no_next_page(self):
        self.document();self.ocr.cancel=True
        self.assertEqual(self.extract(),'cancelled');self.assertEqual(self.ocr.calls,[1])
        self.assertTrue(all(not p.get('ocr') and not p['text'] for p in self.core.task['document']['pages']))

    def test_bad_raw_is_rejected_without_poisoning_page(self):
        for mutate in (lambda raw:raw.update(recognitionComplete=False),lambda raw:raw.update(pixelWidth=3001),
                       lambda raw:raw['observations'][0].update(rect=[599,0,30,10]),
                       lambda raw:raw['observations'][0].update(confidence=float('nan'))):
            with self.subTest(mutate=mutate):
                self.ocr.bad=mutate;self.document(1,1);self.extract();p=self.page()
                self.assertNotIn('ocr',p);self.assertEqual(p['text'],'');encode_snapshot(self.core.task)

    def test_observation_text_budget_is_independent_of_raw_text(self):
        self.document(1,1);self.ocr.bad=lambda raw:raw['observations'][0].update(text='x'*(MAX_INPUT_BYTES+1))
        self.assertEqual(self.extract(),'failed');self.assertNotIn('ocr',self.page())

    def test_revisions_freeze_after_translation_and_fork_carries_no_old_results(self):
        old=self.document();self.extract();self.core.run('translate',threading.Event(),lambda _:None)
        prefix=foundation_json(old);p=self.page()
        with self.assertRaises(CoreError) as e:self.core.draft(1,p['revision'],'new')
        self.assertEqual(e.exception.code,'source_frozen')
        new=self.core.fork_document();self.assertNotEqual(new['task']['id'],old['id'])
        self.assertNotIn('text',new['task']);self.assertEqual(new['task']['document']['segments'],[])
        self.assertEqual(prefix,foundation_json(old));self.assertEqual(self.page()['effectiveSource']['kind'],'inherited-ocr')
        self.core.draft(1,self.page()['revision'],'new');self.core.apply_draft(1,self.page()['revision'])
        self.assertEqual(prefix,foundation_json(old))

    def test_manual_clear_and_input_have_independent_source(self):
        self.document();self.extract();self.core.draft(1,self.page()['revision'],'');self.core.apply_draft(1,self.page()['revision'])
        self.assertEqual(self.page()['effectiveSource']['kind'],'manual-clear');self.assertEqual(self.page()['state'],'noText')
        self.core.fork_document(fresh=True);self.core.draft(1,self.page()['revision'],'Manual only');self.core.apply_draft(1,self.page()['revision'])
        self.assertEqual(self.page()['effectiveSource']['kind'],'manual-input');self.assertNotIn('ocr',self.page())

    def test_raw_prefix_survives_cancel_open_and_only_missing_recognized(self):
        task=self.document();cancel=threading.Event()
        def changed(snapshot):
            if snapshot['task']['document']['pages'][0].get('ocr'):cancel.set()
        self.assertEqual(self.extract(changed,cancel),'cancelled');prefix=foundation_json(self.page())
        self.core.open(task['id']);self.extract();self.assertEqual(self.ocr.calls,[1,2,3]);self.assertEqual(prefix,foundation_json(self.page()))

    def test_draft_save_failure_preserves_disk_and_stops_translation(self):
        task=self.document();self.extract();path=self.store.directory(task['id'])/'current.json';before=path.read_bytes()
        def fail(raw,target):raise OSError('controlled atomic publication failure')
        self.store.write=fail
        with self.assertRaises(CoreError):self.core.draft(1,self.page()['revision'],'unsaved edit')
        self.assertEqual(path.read_bytes(),before);self.assertTrue(self.core.save_blocked)
        self.assertEqual(self.core.task['drafts']['1']['text'],'unsaved edit');self.assertFalse(self.calls)

    def test_wrong_raw_effective_draft_review_and_future_schema_rejected(self):
        task=self.document();self.extract()
        for edit in (lambda t:t['document']['pages'][0]['effectiveSource'].update(rawRevision=str(uuid.uuid4())),
                      lambda t:t['document']['pages'][0].update(reviewed=True,reviewRevision=str(uuid.uuid4())),
                      lambda t:t.update(drafts={'1':dict(id=str(uuid.uuid4()),baseRevision=str(uuid.uuid4()),text='wrong')})):
            t=deepcopy(task);edit(t)
            with self.assertRaises(CoreError):encode_snapshot(t)
        outer=json.loads(encode_snapshot(task));outer['schema']=3
        with self.assertRaises(CoreError):decode_snapshot(json.dumps(outer).encode())


    def test_frozen_schema2_previous_recovery_is_read_only_and_future_never_downgrades(self):
        import base64
        fixture=fixtures.P/'fixtures/b04';manifest=json.loads((fixture/'manifest.json').read_bytes())
        identity=manifest['taskID'];directory=self.store.directory(identity);directory.mkdir()
        for name in ('current.json','previous.json'):(directory/name).write_bytes((fixture/name).read_bytes())
        source=json.loads((fixtures.P/'fixtures/b03/source-bytes.json').read_bytes())
        (directory/'source.pdf').write_bytes(base64.b64decode(source['base64']))
        self.core.open(identity);self.assertEqual(self.core.task['drafts']['2']['text'],'Unapplied controlled draft 34, not 43.')
        before=(directory/'current.json').read_bytes();self.core.export();self.assertEqual(before,(directory/'current.json').read_bytes())
        outer=json.loads(before);outer['schema']=3;(directory/'current.json').write_text(json.dumps(outer))
        with self.assertRaises(CoreError):self.core.open(identity)
        (directory/'current.json').write_bytes(b'controlled corrupt current')
        self.core.open(identity);self.assertTrue(self.core.save_blocked);self.assertEqual(self.core.task['revision'],1)
        with self.assertRaises(CoreError):self.core.draft(1,self.page()['revision'],'new')
