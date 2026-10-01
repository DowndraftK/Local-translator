"""Serial, reproducible local-model trial runner. Plans and complete evidence stay private."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--after', type=Path)
    args=p.parse_args(); plan=json.loads(args.plan.read_text())
    if args.after:
        deadline=time.monotonic()+1200
        while True:
            if args.after.exists():
                state=json.loads(args.after.read_text()).get('state')
                if state in ('completed','failed','stopped','needs_translation'): break
            if time.monotonic()>deadline: raise TimeoutError('Prior trial did not finish')
            time.sleep(1)
    reports=[]
    for trial in plan:
        session=Path(trial['session'])
        if (session/'session.sqlite').exists(): raise ValueError(f'Refusing existing session {session}')
        session.mkdir(parents=True,exist_ok=True)
        if trial.get('pause_sample'):
            sample=trial['pause_sample']
            events=[{'id':'fixture-pause','kind':'paused','sample':sample,'wall_time':1000},
                    {'id':'fixture-resume','kind':'resumed','sample':sample,'wall_time':1095}]
            (session/'capture-events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
        env={**os.environ,'PYTHONPATH':trial.get('runtime','runtime'),'PYTHONDONTWRITEBYTECODE':'1'}
        command=[sys.executable,'-m','streaming_translator','run','--session',str(session),
                 '--config',trial['config'],'--input',trial['input']]
        if trial.get('paced',True): command.append('--paced')
        if not trial.get('translation'):command.append('--no-translation')
        started=time.time()
        with session.with_suffix('.log').open('w') as log:
            result=subprocess.run(command,env=env,stdout=log,stderr=log)
        snapshot=json.loads((session/'snapshot.json').read_text())
        report={'session':str(session),'exit_code':result.returncode,'elapsed':time.time()-started,
                'state':snapshot.get('state'),'asr_complete':snapshot.get('asr_complete')}
        reports.append(report)
        args.plan.with_suffix('.results.json').write_text(json.dumps(reports,indent=2))
        print(json.dumps(report),flush=True)
        if result.returncode or report['state']!='completed' or not report['asr_complete']:
            raise RuntimeError('Trial failed or was interrupted; inspect its saved evidence')


if __name__=='__main__':main()
