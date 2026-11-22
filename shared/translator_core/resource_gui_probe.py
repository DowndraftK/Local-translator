"""Pinned GUI interpreter/Qt facts only; no window, audio or model objects."""
import importlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path


def main():
    request=json.loads(Path(sys.argv[1]).read_bytes())
    assert sys.flags.isolated and sys.dont_write_bytecode
    assert str(Path(sys.prefix))==request['prefix']
    versions={name:importlib.metadata.version(name) for name in ('PySide6-Essentials','shiboken6')}
    import PySide6.QtCore as core
    assert platform.python_version()=='3.12.14' and core.qVersion()=='6.10.2'
    assert all(v=='6.10.2' for v in versions.values())
    origins={}
    for name in ('PySide6','PySide6.QtCore','shiboken6'):
        module=importlib.import_module(name);origin=Path(module.__file__).resolve()
        assert origin.is_relative_to(Path(sys.prefix))
        origins[name]=str(origin)
    print(json.dumps(dict(version='gui-runtime-probe-v1',python=platform.python_version(),executable=sys.executable,
        prefix=sys.prefix,versions=versions,qt=core.qVersion(),origins=origins,models_loaded=False,
        inference=False,window_opened=False,audio_opened=False)))


if __name__=='__main__':main()
