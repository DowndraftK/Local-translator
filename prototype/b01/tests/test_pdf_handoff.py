"""Disposable process handoff attacks; controlled responder is not a PDF engine."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[3]/"shared"),str(Path(__file__).resolve().parents[1])]
from translator_core import CoreError, Cancelled
from translator_core.pdf_adapter import PDFAdapter
from translator_core.contracts import sha
from translator_preview.runtime import owned_python

RESPONDER='''import json,sys,time
from pathlib import Path
q=json.loads(Path(sys.argv[-2]).read_bytes());mode=Path(__file__).with_suffix('.mode').read_text()
r=dict(schema=1,request=q['request'],action=q['action'],backend='controlled-v1',complete=True)
if q['action']!='probe':
 r.update(fingerprint=q['fingerprint'],page=q.get('page'),text='Controlled.')
 if mode=='request':r['request']='old-request'
 if mode=='page':r['page']=999
 if mode=='backend':r['backend']='other'
 if mode=='partial':r['complete']=False
 if mode=='late':time.sleep(3)
Path(sys.argv[-1]).write_text(json.dumps(r))
'''

class HandoffCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        script=self.root/'controlled.py';script.write_text(RESPONDER);self.mode=script.with_suffix('.mode');self.mode.write_text('valid')
        self.adapter=PDFAdapter(self.root/'handoff',[owned_python(),'-I','-B',str(script)])
        self.source=self.root/'controlled.pdf';self.source.write_bytes(b'opaque controlled source')
    def test_wrong_request_page_backend_and_partial_are_rejected_and_released(self):
        for mode in ('request','page','backend','partial'):
            self.mode.write_text(mode)
            with self.subTest(mode=mode),self.assertRaises(CoreError) as error:
                self.adapter.page(self.source,sha(self.source.read_bytes()),1,threading.Event())
            self.assertEqual(error.exception.code,'adapter_identity_mismatch')
            self.assertFalse(list(self.adapter.root.iterdir()))
    def test_wrong_source_hash_stops_before_any_handoff(self):
        with self.assertRaises(CoreError) as error:self.adapter.page(self.source,'0'*64,1,threading.Event())
        self.assertEqual(error.exception.code,'source_mismatch');self.assertFalse(list(self.adapter.root.iterdir()))
    def test_owned_fact_process_cancellation_discards_late_result(self):
        self.mode.write_text('late');cancel=threading.Event();timer=threading.Timer(.2,cancel.set);timer.start()
        try:
            with self.assertRaises(Cancelled):self.adapter.page(self.source,sha(self.source.read_bytes()),1,cancel)
        finally:timer.cancel()
        self.assertFalse(list(self.adapter.root.iterdir()))
