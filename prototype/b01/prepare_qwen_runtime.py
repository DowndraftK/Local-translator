"""Assemble the optional Qwen runtime from manually acquired local wheels."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading

package = Path(__file__).resolve().parent
shared = package/'shared'
if not shared.is_dir():
    shared = package.parents[1]/'shared'
sys.path.insert(0, str(shared))

from translator_core.qwen_runtime import seal
from translator_core.resources import no_links

WHEEL_PINS = {
    'vllm_metal-0.33.0-cp312-cp312-macosx_15_0_arm64.whl':
        'fcf2224250976412a3a8304ec6f48525060f6d30a38c1afad0a97844e03c8dd0',
    'qwen3_asr_causal-0.1.0-py3-none-any.whl':
        'ae060ffcd924f14dc4f9667353bbcabcb7f4d1bbd1be8e29bae9cc828b7cd28e',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheelhouse', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--uv', default=shutil.which('uv'))
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() != 'arm64' or sys.version_info[:2] != (3, 12):
        parser.error('请用 Apple Silicon 上的 Python 3.12。')
    if not args.uv:
        parser.error('需要本机 uv，可用 --uv 指定可执行文件。')
    source = no_links(args.wheelhouse.absolute())
    target = no_links(args.output.absolute())
    if target.exists() or not target.parent.is_dir():
        parser.error('请选择位于现有目录下、尚未建立的输出文件夹。')
    wheels = {p.name: no_links(p) for p in sorted(source.glob('*.whl'))}
    for name, expected in WHEEL_PINS.items():
        if name not in wheels:
            parser.error(f'wheelhouse 缺少 {name}。')
        with wheels[name].open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                parser.error(f'{name} 校验值不匹配。')
    with tempfile.TemporaryDirectory(prefix='.qwen-runtime-', dir=target.parent) as staging:
        root = Path(staging)/'qwen3-metal-mac-arm64-v1'
        root.mkdir()
        site = root/'site-packages'
        env = dict(os.environ, UV_PYTHON_DOWNLOADS='never', UV_OFFLINE='1')
        subprocess.run([args.uv, 'pip', 'install', '--offline', '--no-index', '--no-deps', '--no-cache',
                        '--python', sys.executable, '--target', str(site), *map(str, wheels.values())],
                       env=env, check=True)
        # The isolated interpreter uses -S, so site startup hooks are unnecessary.
        for hook in site.glob('*.pth'):
            hook.unlink()
        for cache in sorted(site.rglob('__pycache__'), reverse=True):
            shutil.rmtree(cache)
        seal(root, threading.Event())
        if target.exists():
            parser.error('输出目录在准备期间已建立。')
        root.rename(target)
    print(target)


if __name__ == '__main__':
    main()
