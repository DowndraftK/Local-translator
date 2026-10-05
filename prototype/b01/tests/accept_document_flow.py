"""B03 real extraction/render/finite translation. This does not count as GUI."""
import argparse
import json
import sys
import threading
from copy import deepcopy
from pathlib import Path

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--materials',type=Path,required=True);parser.add_argument('--task-root',type=Path,required=True)
    parser.add_argument('--evidence-root',type=Path,required=True);parser.add_argument('--real-translation',action='store_true')
    args=parser.parse_args();project=Path(__file__).resolve().parents[3];candidate=args.candidate.resolve()
    package=candidate/'B01' if sys.platform=='win32' else next(candidate.glob('*.app'))/'Contents/Resources/B01'
    root,evidence=args.task_root.resolve(),args.evidence_root.resolve()
    if root.exists() or evidence.exists():raise ValueError('Use new isolated evidence/task directories')
    if not all(p.is_relative_to(project/'artifacts') for p in (root,evidence)):raise ValueError('Use project artifacts roots')
    evidence.mkdir(parents=True);sys.path[:0]=[str(package),str(package/'shared'),str(Path(__file__).parent)]
    from native_peer import NativePeer
    from translator_core.contracts import sha,foundation_json
    from translator_core.store import file_identity
    report=dict(schema=1,candidate=json.loads((candidate/'candidate.json').read_bytes()),native=sys.platform,
                gui_verified=False,cases=[],real_translation=args.real_translation)
    peer=NativePeer(package,root);report['ready']=peer.ready
    def selection(name):
        (evidence/'accept-progress.json').write_text(json.dumps(dict(step='selecting',name=name,cases=len(report['cases']))))
        print('select',name,flush=True)
        event,seconds,_=peer.operate('document.select',source_path=str(args.materials.resolve()/name))
        (evidence/'accept-progress.json').write_text(json.dumps(dict(step='selected',name=name,status=event['status'],cases=len(report['cases']))))
        return event,seconds
    def create(name,first,last,direction='en-zh'):
        event,seconds=selection(name);assert event['status']=='completed',event
        s=event['result']['detail'];peer.command('document.create',selection_token=s['token'],first=first,last=last,direction=direction)
        return s
    def export(label):
        peer.send('task.export');event=peer.until('task.exported');raw=(root/'exports'/event['filename']).read_bytes()
        assert (len(raw),sha(raw))==(event['byte_count'],event['sha256']);value=raw.decode()
        task=peer.saved['task']
        for page in task['document']['pages']:
            assert page['text'] in value
            assert ('PDF物理页 '+str(page['id']) if task['document']['isPDF'] else 'TXT全文') in value
        for part in (task.get('text') or {}).get('segments',[]):
            if part['state']=='completed':assert part['result']['translation'] in value
        (evidence/(label+'.txt')).write_bytes(raw)
        return dict(byteCount=len(raw),sha256=sha(raw),content_asserted=True)
    try:
        s=create('text-12.pdf',1,12);assert s['totalPages']==12
        event,seconds,_=peer.operate('document.extract');assert event['status']=='completed',event
        task=peer.saved['task'];assert len(task['document']['pages'])==12 and 'END-B03-UNIQUE-12' in task['document']['pages'][-1]['text']
        assert task['document']['pages'][0]['text'].count('Do not delete this repeated sentence.')==2
        p=task['document']['pages'][-1];before=(root/task['id']/'current.json').read_bytes()
        render,render_seconds,_=peer.operate('document.render',page=12,page_revision=p['revision']);assert render['status']=='completed',render
        assert (root/task['id']/'current.json').read_bytes()==before
        detail=render['result']['detail'];image=root/'page-handoff'/detail['image_file']
        count,digest=file_identity(image);assert (count,digest)==(detail['image']['byteCount'],detail['image']['sha256'])
        (evidence/'physical-12.png').write_bytes(image.read_bytes());image.unlink();image.parent.rmdir()
        report['cases'].append(dict(name='all-12-last-marker-repeats-original-physical-page',task_id=task['id'],seconds=seconds,
                                   render_seconds=render_seconds,image=detail['image'],export=export('text12-complete-source')))
        for name,first,last in [('range-220.pdf',201,220),('text-12.pdf',3,5),('mixed-empty.pdf',1,3),('bom-unicode.txt',1,1)]:
            create(name,first,last,'zh-en' if name.endswith('.txt') else 'en-zh')
            event,seconds,_=peer.operate('document.extract');assert event['status']=='completed',event
            task=peer.saved['task'];assert [p['id'] for p in task['document']['pages']]==list(range(first,last+1))
            if name=='range-220.pdf':assert task['document']['pages'][1]['state']=='noText' and 'END-RANGE-220' in task['document']['pages'][-1]['text']
            if name.endswith('.txt'):
                expected=json.loads((args.materials/'manifest.json').read_bytes())['utf8_expected'];assert task['document']['pages'][0]['text']==expected
            report['cases'].append(dict(name=name,range=[first,last],seconds=seconds,states=[p['state'] for p in task['document']['pages']],export=export(name.replace('.','-'))))
        for name in ('locked.pdf','broken.pdf','utf16.txt'):
            event,seconds=selection(name);assert event['status']=='failed',event
            report['cases'].append(dict(name=name,explicit_failure=event['result']['detail'],seconds=seconds))
        create('geometry.pdf',1,3);peer.operate('document.extract')
        for p in peer.saved['task']['document']['pages']:
            event,seconds,_=peer.operate('document.render',page=p['id'],page_revision=p['revision']);assert event['status']=='completed',event
            detail=event['result']['detail'];meta=detail['image'];assert max(meta['pixelWidth'],meta['pixelHeight'])<=3000 and meta['pixelWidth']*meta['pixelHeight']<=9000000
            image=root/'page-handoff'/detail['image_file'];(evidence/f"geometry-{p['id']}.png").write_bytes(image.read_bytes());image.unlink();image.parent.rmdir()
            report['cases'].append(dict(name=f"geometry-physical-{p['id']}",seconds=seconds,image=meta))
        if args.real_translation:
            # Three physical pages, never a 220-page model run. Cancel during the
            # second page and retain a byte-exact completed prefix on continuation.
            create('text-12.pdf',3,5);peer.operate('document.extract')
            def prefix(snapshot):
                parts=((snapshot or {}).get('task',{}).get('text') or {}).get('segments',[])
                return len(parts)>1 and parts[0]['state']=='completed' and parts[1]['state']=='processing'
            event,seconds,sent=peer.operate('task.start',cancel_when=prefix);assert sent and event['status']=='cancelled',event
            task=deepcopy(peer.saved['task']);prefix_bytes=foundation_json(task['text']['segments'][0]);plan=foundation_json(task['document']['segments'])
            report['cases'].append(dict(name='real-pdf-cancel',seconds=seconds,task_id=task['id'],export=export('pdf-cancelled')))
            report['first_worker_exit']=peer.close();before=(root/task['id']/'current.json').read_bytes();peer=NativePeer(package,root)
            peer.command('task.open',task_id=task['id']);assert (root/task['id']/'current.json').read_bytes()==before
            event,seconds,_=peer.operate('task.start');assert event['status']=='completed',event
            assert prefix_bytes==foundation_json(peer.saved['task']['text']['segments'][0]);assert plan==foundation_json(peer.saved['task']['document']['segments'])
            report['cases'].append(dict(name='real-pdf-active-resume-prefix-unchanged',seconds=seconds,export=export('pdf-resumed')))
            create('bom-unicode.txt',1,1,'zh-en');peer.operate('document.extract');event,seconds,_=peer.operate('task.start');assert event['status']=='completed',event
            report['cases'].append(dict(name='real-txt-translation',seconds=seconds,export=export('txt-translated')))
        report['final_worker_exit']=peer.close();report['passed']=True
    finally:
        print('writing final report',flush=True)
        (evidence/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (evidence/'trace.json').write_text(json.dumps(peer.trace,ensure_ascii=False,indent=2),encoding='utf-8')
        if peer.process.poll() is None:peer.close()
    print(json.dumps(dict(passed=report.get('passed'),cases=len(report['cases']),native=sys.platform,candidate=report['candidate']['build_id']),ensure_ascii=False))

if __name__=='__main__':main()
