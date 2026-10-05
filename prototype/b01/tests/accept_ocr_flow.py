"""Finite native candidate OCR/repair/resume evidence, explicitly separate from GUI."""
import argparse,json,sys,threading,uuid,time
from pathlib import Path
from copy import deepcopy

def main():
 p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path,required=True);p.add_argument('--materials',type=Path,required=True);p.add_argument('--task-root',type=Path,required=True);p.add_argument('--evidence-root',type=Path,required=True);p.add_argument('--real-translation',action='store_true');a=p.parse_args()
 project=Path(__file__).resolve().parents[3];candidate=a.candidate.resolve();root=a.task_root.resolve();evidence=a.evidence_root.resolve()
 if root.exists() or evidence.exists() or not all(v.is_relative_to(project/'artifacts') for v in (root,evidence)):raise ValueError('New project artifacts roots required')
 evidence.mkdir(parents=True);package=candidate/'B01' if sys.platform=='win32' else next(candidate.glob('*.app'))/'Contents/Resources/B01'
 sys.path[:0]=[str(package),str(package/'shared'),str(Path(__file__).parent)]
 from native_peer import NativePeer
 from translator_core.contracts import foundation_json,sha
 manifest=json.loads((a.materials/'manifest.json').read_bytes());peer=NativePeer(package,root)
 report=dict(schema=1,native=sys.platform,candidate=json.loads((candidate/'candidate.json').read_bytes()),ready=peer.ready,cases=[],gui=False)
 def checkpoint():
  (evidence/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 def export(label):
  peer.send('task.export');event=peer.until('task.exported');raw=(root/'exports'/event['filename']).read_bytes();assert (len(raw),sha(raw))==(event['byte_count'],event['sha256'])
  value=raw.decode();t=peer.saved['task']
  for q in t['document']['pages']:
   assert 'PDF物理页 '+str(q['id']) in value and q['text'] in value
   if q.get('ocr'):assert q['ocr']['rawText'] in value and q['ocr']['rawRevision'] in value
  for q in (t.get('text') or {}).get('segments',[]):
   if q['state']=='completed':assert q['result']['translation'] in value
  (evidence/(label+'.txt')).write_bytes(raw)
  return dict(bytes=len(raw),sha256=sha(raw),content_asserted=True)
 def draft(number,text):
  q=peer.saved['task']['document']['pages'][number-1];body=text.encode();name=uuid.uuid4().hex+'.txt';(root/'incoming'/name).write_bytes(body)
  peer.command('document.draft',page=number,page_revision=q['revision'],input_file=name,byte_count=len(body),sha256=sha(body))
 def reopen():
  nonlocal peer
  t=peer.saved['task'];before=(root/t['id']/'current.json').read_bytes();close=peer.close();peer=NativePeer(package,root);peer.command('task.open',task_id=t['id']);assert before==(root/t['id']/'current.json').read_bytes();return close
 try:
  languages=[('zh','zh-en')] if sys.platform=='win32' else [('en','en-zh'),('zh','zh-en')]
  for lang,direction in languages:
   event,_,_=peer.operate('document.select',source_path=str(a.materials.resolve()/f'ocr-{lang}-3.pdf'));assert event['status']=='completed',event
   peer.command('document.create',selection_token=event['result']['detail']['token'],first=1,last=3,direction=direction,mode='ocr')
   event,seconds,sent=peer.operate('document.ocr',cancel_when=lambda s:bool(s and s['task']['document']['pages'][0].get('ocr')))
   first=foundation_json(peer.saved['task']['document']['pages'][0]);assert sent and event['status']=='cancelled',event
   firstclose=reopen();event,resume_seconds,_=peer.operate('document.ocr');assert event['status']=='completed',event
   assert first==foundation_json(peer.saved['task']['document']['pages'][0])
   t=peer.saved['task'];assert all(q.get('ocr') and not q['reviewed'] for q in t['document']['pages'])
   raw=deepcopy(t['document']['pages']);expected=['\n'.join(lines) for lines in manifest['samples'][lang]]
   report['cases'].append(dict(name='native-'+lang+'-ocr-cancel-reopen-resume',seconds=seconds,resume_seconds=resume_seconds,first_worker_exit=firstclose,task=t['id'],pages=raw,
                               expected=expected,peak_memory='unverified',export=export(lang+'-raw')));checkpoint()
   # Compare raw to the self-authored originals; intentional correction is explicit,
   # and any native recognition mistakes remain in the separate raw record.
   draft(2,expected[1]);draftid=peer.saved['task']['drafts']['2']['id'];close=reopen()
   assert peer.saved['task']['drafts']['2']['id']==draftid
   q=peer.saved['task']['document']['pages'][1];peer.command('document.apply',page=2,page_revision=q['revision'])
   q=peer.saved['task']['document']['pages'][1];peer.command('document.review',page=2,page_revision=q['revision'],reviewed=True)
   assert foundation_json(raw[1]['ocr'])==foundation_json(peer.saved['task']['document']['pages'][1]['ocr'])
   assert peer.saved['task']['document']['pages'][1]['reviewRevision']==peer.saved['task']['document']['pages'][1]['revision']
   render,render_seconds,_=peer.operate('document.render',page=2,page_revision=q['revision']);assert render['status']=='completed',render
   detail=render['result']['detail'];image=root/'page-handoff'/detail['image_file'];assert sha(image.read_bytes())==detail['image']['sha256'];(evidence/(lang+'-physical2.png')).write_bytes(image.read_bytes());image.unlink();image.parent.rmdir()
   report['cases'].append(dict(name=lang+'-draft-reopen-apply-review-original-page',worker_exit=close,render_seconds=render_seconds,image=detail['image'],export=export(lang+'-corrected')));checkpoint()
   if a.real_translation and lang==('zh' if sys.platform=='win32' else 'en'):
    def prefix(s):
     parts=((s or {}).get('task',{}).get('text') or {}).get('segments',[])
     return len(parts)>1 and parts[0]['state']=='completed' and parts[1]['state']=='processing'
    event,seconds,sent=peer.operate('task.start',cancel_when=prefix);assert sent and event['status']=='cancelled',event
    t=peer.saved['task'];prefixbytes=foundation_json(t['text']['segments'][0]);plan=foundation_json(t['document']['segments']);report['cases'].append(dict(name='real-translation-cancel',seconds=seconds,export=export(lang+'-translate-cancel')));checkpoint()
    close=reopen();event,seconds,_=peer.operate('task.start');assert event['status']=='completed',event
    assert prefixbytes==foundation_json(peer.saved['task']['text']['segments'][0]) and plan==foundation_json(peer.saved['task']['document']['segments'])
    assert peer.saved['task']['document']['pages'][0]['reviewed'] is False
    report['cases'].append(dict(name='real-translation-resume-preserved-prefix',seconds=seconds,worker_exit=close,export=export(lang+'-translated')));checkpoint()
  report['final_worker_exit']=peer.close();report['passed']=True;checkpoint()
 finally:
  checkpoint();(evidence/'trace.json').write_text(json.dumps(peer.trace,ensure_ascii=False,indent=2))
  if peer.process.poll() is None:peer.close()
 print(json.dumps(dict(passed=report.get('passed'),cases=len(report['cases']),native=sys.platform),ensure_ascii=False))
if __name__=='__main__':main()
