#!/usr/bin/env python3
"""Trim a NEW release copy using observed imports; preserve original environment."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def files(root):
    return [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--imports',type=Path,required=True)
    args=parser.parse_args(); source=args.input.resolve(); out=args.output.resolve()
    assert not out.exists() and source!=out
    before=files(source); imports=json.loads(args.imports.read_text()); imported=set(imports)
    subprocess.run(['/usr/bin/ditto',str(source),str(out)],check=True)
    licenses=out/'Licenses';licenses.mkdir()
    for path in files(source):
        if any(word in path.name.upper() for word in ['LICENSE','COPYING','NOTICE']):
            destination=licenses/path.relative_to(source);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,destination)
    removed=[]
    def remove(path,reason):
        if not path.exists() and not path.is_symlink():return
        candidates=files(path) if path.is_dir() else [path]
        for item in candidates:
            if item.is_symlink():
                removed.append({'path':str(item.relative_to(out)),'bytes':item.lstat().st_size,'link':str(item.readlink()) if hasattr(item,'readlink') else __import__('os').readlink(item),'reason':reason})
                continue
            removed.append({'path':str(item.relative_to(out)),'bytes':item.stat().st_size,'sha256':sha(item),'reason':reason})
        if path.is_dir() and not path.is_symlink():shutil.rmtree(path)
        else:path.unlink()
    site=out/'Python/lib/python3.12/site-packages'
    # Complete ASR+translation import trace and static App entry points use no
    # test runner, installer, web server, or WebSocket server. Not an ASR backend change.
    distributions=['pytest','pytest_asyncio','iniconfig','pluggy','pip','fastapi','starlette','uvicorn','websockets','python_multipart','annotated_doc']
    modules=distributions+['_pytest','multipart']
    for module in modules:
        assert not any(n==module or n.startswith(module+'.') for n in imported),f'Imported removal candidate: {module}'
        remove(site/module,'not imported by complete fixed App flow; test/install/server-only dependency')
        for p in site.glob(module+'-*.dist-info'):remove(p,'removed distribution metadata; license separately preserved')
    remove(out/'Python/include','C headers for development, no App extension builds')
    remove(out/'Python/lib/pkgconfig','development build configuration')
    remove(out/'Python/share/man','development/manual pages')
    remove(out/'Python/lib/python3.12/idlelib','Python IDE; no product entry point')
    remove(out/'Python/lib/python3.12/ensurepip','runtime never installs Python packages')
    remove(site/'_virtualenv.pth','development venv hook; independent interpreter does not use virtualenv')
    remove(site/'_virtualenv.py','development venv hook; independent interpreter does not use virtualenv')
    for path in list(out.rglob('*')):
        if not path.exists():continue
        if path.is_dir() and path.name in ['tests','test','benchmarks','__pycache__'] and 'Licenses' not in path.parts:
            assert not any(v and str(path.relative_to(out)) in v for v in imports.values()),f'Imported tests: {path}'
            remove(path,'test suite, benchmark or generated cache; no App runtime import')
    for path in (out/'Python/bin').iterdir():
        if not path.name.startswith('python'):remove(path,'dependency command-line development/server/installer entry point; App calls isolated python directly')
    for path in list(site.glob('torch/include'))+list(site.glob('mlx/include'))+list(site.glob('av/include')):
        remove(path,'C/C++ extension development headers; required compiled GPU/decoder libraries retained')
    pinned=json.loads((out/'Code/pinned-source.json').read_text());omitted={}
    for name,expected in pinned['patched_files'].items():
        if name.startswith('whisperlivekit/benchmark/') or name in ['whisperlivekit/test_client.py','whisperlivekit/test_data.py','whisperlivekit/test_harness.py']:
            assert not any(v and v.endswith('/Code/source/'+name) for v in imports.values())
            omitted[name]=expected;remove(out/'Code/source'/name,'sealed test/benchmark tooling; explicit release difference retains original identity')
    release={'schema':1,'original_manifest_sha256':sha(out/'Code/pinned-source.json'),'omitted_test_tools':omitted}
    (out/'Code/release-source.json').write_text(json.dumps(release,indent=2)+'\n')
    shutil.copyfile(Path(__file__).parent/'runtime-selfcheck.py',out/'Code/selfcheck.py')
    component=json.loads((out/'component.json').read_text());component['version']='0.2.10-r2-trimmed'
    excluded=set(n.replace('_','-') for n in distributions)
    component['removed_dependencies']={k:v for k,v in component['dependencies'].items() if k in excluded}
    component['dependencies']={k:v for k,v in component['dependencies'].items() if k not in excluded}
    component['omitted_test_tools']=omitted;component['release_source_sha256']=sha(out/'Code/release-source.json')
    component['complete_baseline']=False
    (out/'component.json').write_text(json.dumps(component,indent=2)+'\n')
    (out/'Licenses/DISTRIBUTION-REVIEW.txt').write_text('''This copy is prepared for personal use on the owner's Mac. Package licenses and notices are retained.\nPublic binary publication is not authorized and is not cleared yet.\nPyAV reports FFmpeg LGPL version 3 or later and ships additional codec libraries. Corresponding-source/build and codec licensing must be completed before public distribution.\nCPython 3.12.14 standalone build 20260825 and all locked versions are unchanged.\n''')
    after=files(out)
    report={'schema':1,'before_files':len(before),'before_bytes':sum(p.stat().st_size for p in before),
            'after_files':len(after),'after_bytes':sum(p.stat().st_size for p in after),'removed':removed,
            'retained_reason':'Runtime dependencies actually imported or not proven unused; torchgen/setuptools/faster-whisper/ctranslate2/tokenizers retained; all GPU and decode dylibs retained',
            'imports_evidence_sha256':sha(args.imports),'omitted_test_tools':omitted}
    (out.parent/(out.name+'-trim-report.json')).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ['removed','omitted_test_tools']},indent=2))

if __name__=='__main__':main()
