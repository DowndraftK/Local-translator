"""Read historical evidence without modifying it; write private diagnostic slices."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import wave
from zoneinfo import ZoneInfo


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args=p.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    db=sqlite3.connect((args.history/'session/session.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    metadata={r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM metadata')}
    cases=[('missing',930,1060),('loop',3280,3385)]
    for name,start,end in cases:
        rows=[dict(r) for r in db.execute('SELECT * FROM segments WHERE end>=? AND start<=?',(start,end))]
        events=[]
        for r in db.execute('SELECT * FROM asr_events ORDER BY sequence'):
            e=json.loads(r['payload'])
            if any(t['end']>=start and t['start']<=end for t in e['tokens']): events.append(dict(r))
        logs=[]
        for line in (args.history/'worker.log').read_text().splitlines():
            try: wall=datetime.strptime(line[:23],'%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
            except ValueError:continue
            # This is input wall elapsed, explicitly NOT an acoustic timestamp.
            elapsed=wall-metadata['playback_started_at']
            if start-10<=elapsed<=end+10 and ('Output:' in line or 'VAD_EVENT' in line or 'guard]' in line):
                logs.append({'input_wall_elapsed':elapsed,'line':line})
        with wave.open(str(args.history/'session/audio.wav'),'rb') as source:
            source.setpos(start*16000)
            with wave.open(str(args.output/(name+'.wav')),'wb') as target:
                target.setparams(source.getparams());target.writeframes(source.readframes((end-start)*16000))
        result={'media_range':[start,end],'samples':[start*16000,end*16000], 'segments':rows,
                'asr_events':events,'logs':logs,'empty_model_outputs':sum(x['line'].endswith('Output: ') for x in logs),
                'vad_late_events_session':metadata.get('vad_late_events'),
                'historical_delivery_accounting':{k:metadata.get(k) for k in ['input_samples','received_pcm_samples','vad_active_samples','vad_silence_samples']}}
        (args.output/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(name, len(rows),len(events),result['empty_model_outputs'])
    db.close()


if __name__=='__main__': main()
