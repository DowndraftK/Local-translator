"""Finite opt-in native candidate peer; full evidence stays in ignored roots."""
import base64
import json
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

class NativePeer:
    def __init__(self, package, root, endpoint='http://127.0.0.1:11434'):
        from translator_preview.protocol import EventTracker
        from translator_preview.runtime import owned_environment, owned_python
        self.package,self.root=Path(package),Path(root);self.session=str(uuid.uuid4())
        self.tracker=EventTracker(self.session);self.events=queue.Queue();self.trace=[];self.saved=None;self.transfer=None
        args=[owned_python(),'-I','-B','-X','utf8',str(self.package/'launch.py'),'worker','--session-id',self.session,
              '--task-root',str(root),'--boundary-helper',str(self.package/'shared/platform/macos/text-boundaries'),
              '--pdf-helper',str(self.package/'shared/platform/macos/pdf-facts'),'--pdf-library',str(self.package/'vendor/pdfium'),
              '--endpoint',endpoint]
        self.process=subprocess.Popen(args,env=owned_environment(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.reader=threading.Thread(target=self.read,daemon=True);self.reader.start()
        self.send('hello');self.ready=self.until('ready')
    def read(self):
        for raw in self.process.stdout:
            try:self.events.put(self.tracker.accept(raw))
            except Exception as exc:self.events.put(exc);return
    def send(self,kind,**fields):
        from translator_preview.protocol import request,encode
        if kind in ('document.extract','document.render','document.ocr','task.start','task.save','task.export') and self.saved:
            fields.setdefault('task_id',self.saved['task']['id']);fields.setdefault('revision',self.saved['task']['revision'])
        req=request(kind,self.session,**fields);self.process.stdin.write(encode(req));self.process.stdin.flush();return req
    def next(self,timeout=1):
        from translator_core.contracts import sha
        e=self.events.get(timeout=timeout)
        if isinstance(e,Exception):raise e
        kind=e['type'];self.trace.append({k:v for k,v in e.items() if k!='data'})
        if kind=='error':raise ValueError(e)
        if kind=='snapshot.begin':self.transfer=(e,bytearray())
        elif kind=='snapshot.chunk':
            assert self.transfer[0]['transfer_id']==e['transfer_id'];self.transfer[1].extend(base64.b64decode(e['data']))
        elif kind=='snapshot.end':
            begin,raw=self.transfer;assert len(raw)==begin['byte_count'] and sha(raw)==begin['sha256']
            self.saved=json.loads(raw);self.transfer=None
        return e
    def until(self,kind,timeout=30):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:e=self.next(min(1,max(.01,deadline-time.monotonic())))
            except queue.Empty:continue
            if e['type']==kind:return e
        raise TimeoutError(kind)
    def command(self,kind,**fields):
        self.send(kind,**fields);return self.until('task.done')
    def operate(self,kind,cancel_when=None,**fields):
        op,lease=str(uuid.uuid4()),str(uuid.uuid4());start=time.monotonic();renew=start+4;cancelled=False
        self.send(kind,operation_id=op,lease_id=lease,lease_ms=20000,**fields)
        while time.monotonic()-start<600:
            now=time.monotonic()
            if not cancelled and now>=renew:self.send('lease.renew',operation_id=op,lease_id=lease);renew=now+4
            if not cancelled and cancel_when and cancel_when(self.saved):
                self.send('cancel',operation_id=op,lease_id=lease);cancelled=True
            try:e=self.next(.05)
            except queue.Empty:continue
            if e['type']=='operation.finished':
                assert e['tail_drained'];return e,time.monotonic()-start,cancelled
        raise TimeoutError(kind)
    def close(self):
        self.send('shutdown');ack=self.until('shutdown.confirmed');code=self.process.wait(timeout=10)
        assert ack['tail_drained'] and code==0;self.reader.join(2)
        error=self.process.stderr.read().decode('utf-8','replace')
        for stream in (self.process.stdin,self.process.stdout,self.process.stderr):stream.close()
        return dict(actual_worker_pid=self.process.pid,exit_code=code,tail_drained=True,stderr=error)
