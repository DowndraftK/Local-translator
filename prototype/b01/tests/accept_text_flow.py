"""Opt-in finite native candidate acceptance. Uses existing, fixed local models only.
Detailed task/result evidence remains in ignored artifacts; this is not a quality study.
"""
import base64,json,queue,subprocess,sys,threading,time,uuid,hashlib
from pathlib import Path
import argparse
parser=argparse.ArgumentParser(description='Finite real-model worker flow on the native built B02 candidate')
parser.add_argument('--candidate',type=Path,required=True)
parser.add_argument('--task-root',type=Path,required=True)
parser.add_argument('--evidence-root',type=Path,required=True)
args=parser.parse_args()
P=Path(__file__).resolve().parents[3]
C=args.candidate.resolve()
PACKAGE=C/('B01' if sys.platform=='win32' else '本地翻译器 B02预览.app/Contents/Resources/B01')
ROOT=args.task_root.resolve()
EVIDENCE=args.evidence_root.resolve()
if ROOT.exists(): raise ValueError('Use a new acceptance task root; original tasks must stay intact')
if not ROOT.is_relative_to(P/'artifacts') or not EVIDENCE.is_relative_to(P/'artifacts'): raise ValueError('Use project-local ignored evidence directories')
EVIDENCE.mkdir(parents=True,exist_ok=True)
sys.path[:0]=[str(PACKAGE),str(PACKAGE/'shared')]
from translator_preview.protocol import encode,request,EventTracker
from translator_core.contracts import decode_snapshot,foundation_json,sha
report={'schema':1,'candidate':json.loads((C/'candidate.json').read_bytes()),'cases':[],'windows_tested':sys.platform=='win32'}
class Peer:
 def __init__(self):
  self.session=str(uuid.uuid4());self.tracker=EventTracker(self.session);self.events=queue.Queue();self.snapshots=[];self.trace=[];self.transfer=None
  self.process=subprocess.Popen([sys.executable,'-I','-B',str(PACKAGE/'launch.py'),'worker','--session-id',self.session,'--task-root',str(ROOT),'--boundary-helper',str(PACKAGE/'shared/platform/macos/text-boundaries')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  self.reader=threading.Thread(target=self.read,daemon=True);self.reader.start();self.send('hello');self.until('ready')
 def read(self):
  for raw in self.process.stdout:
   try:self.events.put(self.tracker.accept(raw))
   except Exception as e:self.events.put(e);return
 def send(self,kind,**fields):
  req=request(kind,self.session,**fields);self.process.stdin.write(encode(req));self.process.stdin.flush();return req
 def next(self,timeout=1):
  e=self.events.get(timeout=timeout)
  if isinstance(e,Exception):raise e
  k=e['type'];self.trace.append({key:v for key,v in e.items() if key not in ('data','protocol','diagnostics')})
  if k=='error':raise AssertionError(e)
  if k=='snapshot.begin':self.transfer=(e,bytearray())
  elif k=='snapshot.chunk':
   assert self.transfer[0]['transfer_id']==e['transfer_id'];self.transfer[1].extend(base64.b64decode(e['data']))
  elif k=='snapshot.end':
   begin,raw=self.transfer;assert len(raw)==begin['byte_count'] and sha(raw)==begin['sha256'];self.snapshots.append(json.loads(raw));self.transfer=None
  return e
 def until(self,kind,timeout=30):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   try:e=self.next(min(1,end-time.monotonic()))
   except queue.Empty:continue
   if e['type']==kind:return e
  raise TimeoutError(kind)
 def create(self,source,direction):
  raw=source.encode();name=uuid.uuid4().hex+'.txt';(ROOT/'incoming'/name).write_bytes(raw)
  self.send('task.create',input_file=name,sha256=sha(raw),byte_count=len(raw),direction=direction);self.until('task.done');return self.snapshots[-1]['task']
 def run(self,cancel_after_prefix=False):
  operation=str(uuid.uuid4());lease=str(uuid.uuid4());start=time.monotonic();next_renew=start+4;cancel_at=None;sent=False
  self.send('task.start',operation_id=operation,lease_id=lease,lease_ms=20000)
  while time.monotonic()-start<600:
   now=time.monotonic()
   if not sent and now>=next_renew:self.send('lease.renew',operation_id=operation,lease_id=lease);next_renew=now+4
   if cancel_after_prefix and not sent and self.snapshots:
    t=self.snapshots[-1].get('task') or {};parts=(t.get('text') or {}).get('segments',[])
    if len(parts)>1 and parts[0]['state']=='completed' and parts[1]['state']=='processing' and cancel_at is None:cancel_at=now+.75
    if cancel_at is not None and now>=cancel_at:self.send('cancel',operation_id=operation,lease_id=lease);sent=True
   try:e=self.next(.1)
   except queue.Empty:continue
   if e['type']=='operation.finished':
    assert e['tail_drained'];return e,self.snapshots[-1]['task'],time.monotonic()-start,sent
  raise TimeoutError('text run')
 def export(self,task,label):
  self.send('task.export');e=self.until('task.exported');raw=(ROOT/'exports'/e['filename']).read_bytes();assert sha(raw)==e['sha256'] and len(raw)==e['byte_count']
  value=raw.decode()
  for part in task['text']['segments']:
   assert part['source'] in value
   if part['state']=='completed':assert part['result']['translation'] in value
  (P/f'artifacts/b02-20261004/{label}-bilingual.txt').write_bytes(raw);return {'bytes':len(raw),'sha256':sha(raw)}
 def close(self):
  self.send('shutdown');ack=self.until('shutdown.confirmed');code=self.process.wait(timeout=8);assert ack['tail_drained'] and code==0
  self.reader.join(2);error=self.process.stderr.read().decode()
  for s in (self.process.stdin,self.process.stdout,self.process.stderr):s.close()
  return {'session':self.session,'worker_pid':self.process.pid,'exit_code':code,'stderr':error,'event_count':self.tracker.sequence}
peer=Peer()
try:
 for label,source,direction in [('english-short','The local task contains 12 notes. Save them before Friday.','en-zh'),('chinese-short','本地任务包含12条记录。请先保存，再核对结果。','zh-en')]:
  print('begin',label,flush=True);peer.create(source,direction);event,task,seconds,_=peer.run();assert event['status']=='completed';assert all(p['state']=='completed' for p in task['text']['segments'])
  exp=peer.export(task,label);report['cases'].append({'name':label,'direction':direction,'task_id':task['id'],'segments':len(task['text']['segments']),'completed':True,'seconds':round(seconds,3),'configuration':task['configuration'],'result_records':[p['result'] for p in task['text']['segments']],'export':exp});print('passed',label,round(seconds,2),flush=True)
 print('begin long-cancel',flush=True)
 source=('The note number 12 stays on this computer. The user can review the saved text later.\n'*48)
 peer.create(source,'en-zh');event,task,seconds,sent=peer.run(True);assert sent and event['status']=='cancelled',event
 parts=task['text']['segments'];assert parts[0]['state']=='completed' and parts[1]['state']=='stopped';prefix=foundation_json(parts[0]);plan=[(p['source'],p['utf8Start'],p['utf8End']) for p in parts];tid=task['id'];raw_before=(ROOT/tid/'current.json').read_bytes()
 report['long_cancel']={'task_id':tid,'source_bytes':len(source.encode()),'segments':len(parts),'completed_prefix':1,'seconds':seconds,'status':event['status'],'prefix_sha256':sha(prefix),'cancel_delay_after_second_processing_seconds':.75,'partial_export':peer.export(task,'long-stopped')}
 report['first_worker']=peer.close();peer=Peer()
 peer.send('task.open',task_id=tid);peer.until('task.done');reopened=peer.snapshots[-1]['task'];assert (ROOT/tid/'current.json').read_bytes()==raw_before
 assert foundation_json(reopened['text']['segments'][0])==prefix
 report['reopen_no_write']=True
 print('begin long-resume',flush=True);event,task,seconds,_=peer.run();assert event['status']=='completed';assert foundation_json(task['text']['segments'][0])==prefix
 assert [(p['source'],p['utf8Start'],p['utf8End']) for p in task['text']['segments']]==plan
 report['long_resume']={'completed':True,'seconds':seconds,'same_plan':True,'prefix_byte_identical':True,'export':peer.export(task,'long-completed'),'result_records':[p['result'] for p in task['text']['segments']]};print('passed long-resume',round(seconds,2),flush=True)
 print('begin legacy-v1',flush=True)
 old=P/'fixtures/b01/text-task/11111111-2222-4333-8444-555555555555';before={p.name:sha(p.read_bytes()) for p in old.iterdir()}
 peer.send('task.import',source_directory=str(old));peer.until('task.done');legacy=peer.snapshots[-1]['task'];first=foundation_json(legacy['text']['segments'][0]);recipe=foundation_json(legacy['configuration'])
 event,task,seconds,_=peer.run();assert event['status']=='completed';assert foundation_json(task['text']['segments'][0])==first and foundation_json(task['configuration'])==recipe
 assert before=={p.name:sha(p.read_bytes()) for p in old.iterdir()};report['legacy_v1']={'completed':True,'seconds':seconds,'old_source_unchanged':True,'prefix_byte_identical':True,'configuration_byte_identical':True,'configuration':task['configuration'],'new_result':task['text']['segments'][1]['result'],'export':peer.export(task,'legacy-v1')};print('passed legacy-v1',round(seconds,2),flush=True)
 report['second_worker']=peer.close();peer=None
 report['confirmed_real_translation_segments']=5
 report['all_passed']=True
finally:
 if peer and peer.process.poll() is None:
  try:report['cleanup_worker']=peer.close()
  except Exception as e:report['cleanup_error']=str(e)
 (EVIDENCE/'mac-worker-real-flow.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print('all real-flow checks passed',flush=True)
