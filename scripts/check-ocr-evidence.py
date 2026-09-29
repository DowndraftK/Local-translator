"""Inspect saved opt-in OCR acceptance evidence without running models.
Usage: python3 scripts/check-ocr-evidence.py artifacts/ocr-YYYYMMDD
Outputs contain summaries only; the artifact directory remains ignored.
"""
import json, sys, re, difflib
from pathlib import Path
root = Path(sys.argv[1])
def read(name): return json.loads((root/name).read_text())
def check_source(s):
    for page in s['pages']:
        mapping = [m for m in s['sources'] if m['page'] == page['id']]
        if not mapping:
            assert not page['text'].strip() or page['state'] in ['pending','stopped']
            continue
        assert ''.join(s['segments'][m['segmentID']]['source'] for m in mapping) == page['text']
        for m in mapping:
            segment = s['segments'][m['segmentID']]
            assert m['revision'] == page['revision']
            assert segment['source'].encode() == page['text'].encode()[m['utf8Start']:m['utf8End']]
            assert len(segment['source'].strip().encode()) <= 2048
        if page.get('ocr'):
            assert page['ocr']['pixelWidth'] <= 3000 and page['ocr']['pixelHeight'] <= 3000
            assert page['ocr']['pixelWidth'] * page['ocr']['pixelHeight'] <= 9_000_000

report = {'documents': {}, 'ui': {}}
for name in ['scan-12','chinese-scan','public-raster']:
    s = read(name+'-final-source.json'); t = read(name+'-translation.json'); check_source(s)
    assert t['source'] == s['id'] + ''.join(p['revision'] for p in s['pages'])
    assert t['phase'] == 'completed'
    output = (root/(name+'-complete.txt')).read_text()
    assert re.findall(r'===== PDF 物理页 (\d+)',output) == [str(p['id']) for p in s['pages']]
    for page in s['pages']:
        raw = '\n'.join(o['text'] for o in page.get('ocr',{}).get('observations',[]))
        assert raw in output and page['text'] in output and page['revision'] in output
    assert all(part['state']=='completed' and part['result']['translation'] in output for part in t['segments'])
    report['documents'][name] = {'pages':len(s['pages']), 'segments':len(t['segments']), 'sourceCharacters':sum(len(p['text']) for p in s['pages']), 'allPagesAndVersionsExported':True}
known = read('known.json'); scan = read('scan-12-ocr.json')
report['knownOCR'] = []
for page, expected in zip(scan['pages'], known):
    a=' '.join(expected.split()); b=' '.join(page['text'].split())
    changes=[{'operation':tag,'expected':a[i:j],'recognized':b[k:l]} for tag,i,j,k,l in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes() if tag != 'equal']
    report['knownOCR'].append({'page':page['id'],'exactEqual':page['text']==expected,'normalizedChanges':changes})
for file in ['scan-12-stopped.txt','scan-12-OCR-stopped.txt']:
    text=(root/file).read_text(); assert re.findall(r'===== PDF 物理页 (\d+)',text)==[str(n) for n in range(1,13)]
quality=read('quality-ocr.json');assert [p['state'] for p in quality['pages']]==['noText','noText']
assert re.findall(r'===== PDF 物理页 (\d+)',(root/'quality-zero.txt').read_text())==['1','2']
mixed=read('mixed-ocr.json'); assert mixed['pages'][0]['text'].count('VISIBLE HEADER ONLY')==1
assert read('mixed-text-layer.json')['pages'][0]['text'].strip()=='VISIBLE HEADER ONLY'
geometry=read('geometry-ocr.json');assert 'END-MARKER' not in geometry['pages'][1]['text'];assert geometry['pages'][2]['ocr']['pixelHeight']==3000
for filename,pages,count in [('ui-build13-stopped.txt',list(range(1,13)),5),('ui-build13-range10-12-complete.txt',[10,11,12],3)]:
    s=(root/filename).read_text();assert re.findall(r'===== PDF 物理页 (\d+)',s)==list(map(str,pages));assert f'已完成 {count}' in s
    report['ui'][filename]={'allPagesPresent':True,'completed':count}
report['ui']['copy']=read('ui-copy-check.json')
(root/'evidence-check-final.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'documents':report['documents'],'knownPagesExactlyEqual':sum(x['exactEqual'] for x in report['knownOCR']),'ui':report['ui']},ensure_ascii=False,indent=2))
