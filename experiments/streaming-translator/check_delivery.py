"""Validate complete saved rows, media, revisions and all export time definitions."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import wave

from streaming_translator.store import export_subtitles, SessionStore, file_sha256


def stamp(value):
    h,m,s=re.split('[:]',value)
    return int(h)*3600000+int(m)*60000+round(float(s.replace(',','.'))*1000)


def check(path):
    store=SessionStore(path)
    try:
        s=store.snapshot(); rows=s['segments']
        with wave.open(str(path/'audio.wav'),'rb') as audio:
            count=audio.getnframes(); duration=count/audio.getframerate()
        assert s.get('input_samples',count)==count
        assert all(0<=r['start']<r['end']<=duration for r in rows)
        assert store.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not s.get('audio_sha256') or file_sha256(path/'audio.wav')==s['audio_sha256']
        assert store.db.execute('SELECT COUNT(*) FROM translations t JOIN segments s ON t.segment_id=s.id WHERE t.revision!=s.revision').fetchone()[0]==0
        for kind in ('txt','srt','vtt'):
            text=export_subtitles(s,kind)
            (path/f'checked-subtitles.{kind}').write_text(text)
            if kind=='txt':
                times=re.findall(r'^\[([\d.]+)–([\d.]+)\]$',text,re.M)
                assert len(times)==len(rows)
                assert all((a,b)==(f"{r['start']:.2f}",f"{r['end']:.2f}") for (a,b),r in zip(times,rows))
            else:
                times=re.findall(r'^(\d+:\d+:\d+[,.]\d+) --> (\d+:\d+:\d+[,.]\d+)$',text,re.M)
                assert len(times)==len(rows)
                assert all((stamp(a),stamp(b))==(round(r['start']*1000),round(r['end']*1000)) for (a,b),r in zip(times,rows))
        return {'session':str(path),'state':s['state'],'segments':len(rows),'samples':count,
            'all_three_exports_match_effective_times':True, 'translation_counts':s['translation_counts'],
            'audio_hash_matches':not s.get('audio_sha256') or file_sha256(path/'audio.wav')==s['audio_sha256'],
            'timing_needs_review':sum(r['timing_status']=='needs_review' for r in rows)}
    finally:store.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session',type=Path,action='append',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();r=[check(s) for s in a.session]
    a.output.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r))


if __name__=='__main__':main()
