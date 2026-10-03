"""Content-checked relocation; never rewrite an old task's runtime.json.

The sealed Python bytes stay unchanged. Scoped path adapters redirect only
asset lookup in the four pinned modules; their code origin remains inspectable.
"""
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .store import file_sha256


def managed_root():
    value = os.environ.get('LOCAL_TRANSLATOR_RUNTIME_ROOT')
    return Path(value).resolve() if value else None


def validate_source_files(source, fingerprints):
    """Keep the old complete identity; explicitly account for removed test tools."""
    omitted = {}
    root = managed_root()
    if root is not None and (root/'Code/release-source.json').is_file():
        path = root/'Code/release-source.json'
        component = json.loads((root/'component.json').read_text())
        if file_sha256(path) != component['release_source_sha256']:
            raise ValueError('发行源码差异清单校验失败')
        release = json.loads(path.read_text())
        if release['original_manifest_sha256'] != file_sha256(root/'Code/pinned-source.json'):
            raise ValueError('发行源码不属于原封存身份')
        omitted = release['omitted_test_tools']
        for name, expected in omitted.items():
            allowed = name.startswith('whisperlivekit/benchmark/') or name in (
                'whisperlivekit/test_client.py', 'whisperlivekit/test_data.py', 'whisperlivekit/test_harness.py')
            if not allowed or fingerprints['patched_files'].get(name) != expected or (source/name).exists():
                raise ValueError('发行测试工具差异不合法')
    for name, expected in fingerprints['patched_files'].items():
        if name in omitted:
            continue
        if file_sha256(source/name) != expected:
            raise ValueError(f'流式源码校验失败：{name}')


def resolve_config(config, expected_digest=None):
    root = managed_root()
    if root is None:
        return config
    result = dict(config)
    for key, name in [('source_manifest', 'pinned-source.json'), ('model_manifest', 'model-manifest.json')]:
        selected = root/'Code'/name
        trusted = json.loads(selected.read_text())
        original = Path(config[key])
        try:
            previous = json.loads(original.read_text())
        except (FileNotFoundError, PermissionError):
            # A moved task may retain paths denied by the new installation's
            # permissions. Only a saved content identity can permit relocation.
            previous = None
        if previous is not None and previous != trusted:
            raise ValueError('旧任务资源清单与已安装固定组合不同；保留内容，请恢复原资源。')
        if previous is None and expected_digest is None:
            raise ValueError('缺少旧资源身份证据，不能仅凭路径猜测兼容；仍可读取或导出任务。')
        result[key] = str(selected)
    result['source'] = str(root/'Code/source')
    model = os.environ.get('LOCAL_TRANSLATOR_MODEL_PATH')
    if model:
        result['model'] = str(Path(model).resolve())
    if expected_digest:
        from .capture import resource_digest
        if resource_digest(result) != expected_digest:
            raise ValueError('重新定位后的资源/配置指纹不配，拒绝继续，已保存结果保持。')
    return result


def validate_assets():
    root = managed_root()
    if root is None:
        return None
    location = os.environ.get('LOCAL_TRANSLATOR_ASSETS_ROOT')
    if not location:
        raise ValueError('尚未选择外部语音分词/VAD资源。')
    assets = Path(location).resolve()
    manifest = json.loads((root/'Code/assets-manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = assets/name
        if not path.is_file() or path.stat().st_size != expected['size'] or file_sha256(path) != expected['sha256']:
            raise ValueError(f'外部语音资源缺失或校验失败：{name}')
    return assets


def _scope_os_asset_directory(module, directory):
    original_file = module.__file__
    original_os = module.os
    def dirname(value):
        if value == original_file:
            return str(directory)
        return original_os.path.dirname(value)
    path = SimpleNamespace(**{**vars(original_os.path), 'dirname': dirname})
    module.os = SimpleNamespace(**{**vars(original_os), 'path': path})


def apply_asset_mapping(scope='streaming'):
    if scope not in ('streaming', 'refinement'):
        raise ValueError('未知语音资产使用范围')
    assets = validate_assets()
    if assets is None:
        return
    modules = [('mlx_whisper.audio', 'mlx'), ('mlx_whisper.tokenizer', 'mlx')]
    if scope == 'streaming':
        modules += [('whisperlivekit.whisper.audio', 'wlk/whisper'),
                    ('whisperlivekit.whisper.tokenizer', 'wlk/whisper')]
    for name, directory in modules:
        module = importlib.import_module(name)
        if not getattr(module, '_local_assets_mapped', False):
            _scope_os_asset_directory(module, assets/directory)
            module._local_assets_mapped = True
    if scope == 'refinement':
        return
    vad = importlib.import_module('whisperlivekit.silero_vad_iterator')
    if not getattr(vad, '_local_assets_mapped', False):
        original = vad._get_onnx_model_path
        def external_onnx(model_path=None, opset_version=16):
            if model_path is None:
                name = 'silero_vad.onnx' if opset_version == 16 else f'silero_vad_16k_op{opset_version}.onnx'
                model_path = assets/'wlk/silero_vad_models'/name
            return original(model_path, opset_version)
        vad._get_onnx_model_path = external_onnx
        vad._local_assets_mapped = True
