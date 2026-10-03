#!/usr/bin/env python3
"""Sign the new runtime copy, inventory exact bytes, and make a software-only ZIP."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import zipfile

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--version',default='0.2.10-r5')
    args=p.parse_args();root=args.runtime.resolve();args.output.mkdir(parents=True,exist_ok=True)
    executable=[];inventory={}
    magic=[bytes.fromhex(v) for v in ['cffaedfe','cefaedfe','feedfacf','feedface','cafebabe','bebafeca','cafebabf','bfbafeca']]
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():continue
        assert '__pycache__' not in path.parts and path.suffix not in ['.pyc','.pyo'],f'Generated cache in release: {path}'
        with path.open('rb') as f:is_macho=f.read(4) in magic
        if is_macho:
            subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(path)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            subprocess.run(['/usr/bin/codesign','--verify','--strict',str(path)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            arch=subprocess.run(['/usr/bin/lipo','-archs',str(path)],check=True,capture_output=True,text=True).stdout.strip()
            assert 'arm64' in arch,str(path)
            executable.append({'path':str(path.relative_to(root)),'architectures':arch})
    manifest=json.loads((root/'component.json').read_text());manifest['version']=args.version
    manifest['platform']='macos-arm64';manifest['license_status']='reviewed-personal-use-only'
    manifest['macho']=executable
    for path in sorted(root.rglob('*')):
        name=str(path.relative_to(root))
        if path.is_symlink():
            link=os.readlink(path);assert not link.startswith('/') and path.resolve().is_relative_to(root)
            inventory[name]={'link':link}
        elif path.is_file() and name!='component.json':
            assert path.suffix not in ['.onnx','.jit','.tiktoken','.safetensors','.pt'],f'Model data in software: {path}'
            assert path.name not in ['tokenizer.json','config.json','weights.bin'],f'Model data in software: {path}'
            inventory[name]={'size':path.stat().st_size,'sha256':sha(path)}
    manifest['files']=inventory
    (root/'component.json').write_text(json.dumps(manifest,indent=2)+'\n')
    folder='speech-runtime-'+args.version;archive=args.output/(folder+'-macos-arm64.zip')
    assert not archive.exists()
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        for path in sorted(root.rglob('*')):
            name=folder+'/'+str(path.relative_to(root))
            if path.is_symlink():
                info=zipfile.ZipInfo(name);info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
                z.writestr(info,os.readlink(path).encode())
            elif path.is_file():z.write(path,name)
    installed=sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink())
    component={'id':'speech-runtime','version':manifest['version'],'platform':'macos-arm64',
               'archiveSHA256':sha(archive),'archiveBytes':archive.stat().st_size,'installedBytes':installed+4096,
               'manifestSHA256':sha(root/'component.json'),'downloadURL':None,'allowedDownloadHosts':[],
               'archiveRoot':folder,'executable':'Python/bin/python3.12','selfcheck':'Code/selfcheck.py',
               'licenseStatus':'reviewed-personal-use-only'}
    catalog={'schema':1,'components':[component,{'id':'ollama-engine','version':'0.35.0','platform':'macos-arm64',
       'archiveSHA256':'3458972dfca5b3f8a4297a8d7ff503da3e180ec9452ba8de82c69c5ba3aacdd9',
       'archiveBytes':198866136,'installedBytes':629347428,
       'manifestSHA256':'d0dfe238c17c27460fbd7db79d491de5a1e0fea8af80da8d0c2627828713a441',
       'downloadURL':'https://github.com/ollama/ollama/releases/download/v0.35.0/Ollama-darwin.zip',
       'allowedDownloadHosts':['github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'],
       'archiveRoot':'Ollama.app','executable':'Contents/Resources/ollama','selfcheck':None,'licenseStatus':'reviewed'}]}
    (args.output/'component-catalog.json').write_text(json.dumps(catalog,indent=2)+'\n')
    print(json.dumps({'archive':str(archive),'archive_bytes':archive.stat().st_size,'sha256':sha(archive),
                      'installed_bytes':installed,'files':len(inventory)+1,'signed_macho':len(executable),'catalog':str(args.output/'component-catalog.json')},indent=2))

if __name__=='__main__':main()
