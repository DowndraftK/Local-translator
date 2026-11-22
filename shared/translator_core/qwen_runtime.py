"""Offline sealing and verification for the separately installed Metal packages."""
import hashlib
from importlib.metadata import distributions
import os
from pathlib import Path
import stat
import time

from . import CoreError
from .resources import (CHECK_SECONDS, FILE_COUNT, SET_LIMIT, atomic_json, json_value, no_links,
                        open_readonly, relative_name, safe_read, stop_check, verify_resource)
from .speech_presets import QWEN_PACKAGES, QWEN_RUNTIME

RUNTIME_SOURCE = 'https://github.com/vllm-project/vllm-metal/releases/tag/v0.33.0'
MANIFEST = 'component.json'


def versions(root):
    result = {}
    for dist in distributions(path=[str(root/'site-packages')]):
        name = dist.metadata['Name'].lower().replace('_', '-')
        if name in result:
            raise CoreError(f'Qwen 运行包包含重复组件 {name}。', 'runtime_version')
        result[name] = dist.version
    for name, version in QWEN_PACKAGES.items():
        if result.get(name) != version:
            raise CoreError(f'Qwen 运行包需要 {name} {version}，找到 {result.get(name) or "缺失"}。', 'runtime_version')
    return result


def outside_model(name):
    parts = Path(name).parts
    path = Path(name)
    if name == 'site-packages/scipy/stats/_sobol_direction_numbers.npz':
        return True  # SciPy numerical constants.
    return (path.suffix.lower() not in ('.safetensors', '.gguf', '.pt', '.pth', '.ckpt', '.onnx', '.jit',
                                       '.tiktoken', '.npz', '.npy', '.whl', '.zip', '.tar', '.gz', '.mlmodel')
            and path.name.lower() not in ('tokenizer.json', 'vocab.json', 'merges.txt', 'preprocessor_config.json',
                                         'pytorch_model.bin', 'model.safetensors.index.json')
            and not any(part.lower() in ('.cache', 'hf', 'snapshots', 'blobs', '__pycache__') for part in parts))


def spec_at(root):
    root = no_links(root)
    path = root/MANIFEST
    try:
        raw = safe_read(path)
    except FileNotFoundError as exc:
        raise CoreError(f'运行目录缺少 component.json。请准备并封存 {QWEN_RUNTIME} 后导入。',
                        'runtime_version') from exc
    value = json_value(raw)
    files = value.get('files', {})
    if (value.get('version') != QWEN_RUNTIME or value.get('platform') != 'macos-arm64' or
            not isinstance(files, dict) or not files or
            any(not name.startswith('site-packages/') or not outside_model(name) for name in files)):
        raise CoreError('请选择版本匹配且仅包含运行依赖的 Qwen 运行包目录。', 'runtime_version')
    return dict(resource_id='speech.qwen-runtime', role='Qwen Metal 运行组件', root=str(root), version=QWEN_RUNTIME,
                platform='macos-arm64', architecture='arm64', origin=RUNTIME_SOURCE, license='See package licenses',
                ownership='external_readonly', distribution_status='user-import', link_policy='deny',
                manifest=dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), format='component-v1'))


def verify_runtime(root, expected, cancel, progress):
    spec = spec_at(root)
    if spec['manifest']['sha256'] != expected:
        raise CoreError('Qwen 运行包清单已改变，请重新导入。', 'runtime_version')
    facts = verify_resource(spec, cancel, time.monotonic()+CHECK_SECONDS, progress)
    declared = json_value(safe_read(Path(root)/MANIFEST))['dependencies']
    if versions(Path(root)) != declared:
        raise CoreError('Qwen 运行组件实际版本与封存清单不符。', 'runtime_version')
    return spec, facts


def seal(root, cancel):
    root = no_links(Path(root).absolute())
    if (root/MANIFEST).exists():
        raise CoreError('请选择新的 Qwen 运行包目录。', 'runtime_version')
    deps = versions(root)
    files = {}
    total = 0
    deadline = time.monotonic() + CHECK_SECONDS
    for path in sorted((root/'site-packages').rglob('*')):
        stop_check(cancel, deadline)
        no_links(path)
        if path.is_dir():
            continue
        name = relative_name(path.relative_to(root).as_posix())
        if not outside_model(name):
            raise CoreError(f'运行包请移除模型资源、归档或缓存文件 {name}。', 'runtime_version')
        with open(open_readonly(path), 'rb') as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise CoreError(f'运行包文件类型无效 {name}。', 'runtime_version')
            total += st.st_size
            if len(files) >= FILE_COUNT or total > SET_LIMIT:
                raise CoreError('运行包目录超过可核验容量。', 'runtime_version')
            files[name] = dict(size=st.st_size, sha256=hashlib.file_digest(stream, 'sha256').hexdigest())
    atomic_json(root/MANIFEST, dict(schema=1, version=QWEN_RUNTIME, platform='macos-arm64',
                                   dependencies=deps, files=files))


if __name__ == '__main__':
    import argparse
    import threading
    parser = argparse.ArgumentParser(description='离线封存已经准备好的 Qwen Metal 运行依赖')
    parser.add_argument('folder', type=Path)
    seal(parser.parse_args().folder, threading.Event())
