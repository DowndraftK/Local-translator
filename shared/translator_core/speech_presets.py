"""Mac speech preset and runtime identities shared by the UI and worker."""
from pathlib import Path

from .resources import ResourceError, hash_value, manifest_entries, safe_read
from .contracts import sha
from .resource_macos import DEFAULT_WHISPER, LEGACY_WHISPER, SMALL_MODEL_MANIFEST, TRUSTED_MANIFESTS

MODES = {
    'speech.continuous_asr': '同传',
    'speech.file_asr': '录音转写',
    'speech.refine': '录后精修',
}
WHISPER_RUNTIME = 'WhisperLiveKit 363e4f6 · 0.2.10-r5'
QWEN_RUNTIME = 'qwen3-metal-mac-arm64-v1'
QWEN_PACKAGES = {
    'vllm-metal': '0.33.0',
    'vllm': '0.33.0+cpu',
    'qwen3-asr-causal': '0.1.0',
    'mlx': '0.32.4',
    'transformers': '5.20.0',
}
QWEN_BACKEND = 'qwen3-vllm-metal'
WHISPER_BACKEND = 'whisperlivekit'
WLK_SOURCE = '363e4f6d029694d9c81ae548beddd9d3c88a3637'

PRESETS = {
    DEFAULT_WHISPER: dict(name='Whisper small.en', backend=WHISPER_BACKEND, precision='MLX Safetensors · FP16',
                         languages=('en',), modes=tuple(MODES), manifest='whisper-small.en.model.json',
                         pin=SMALL_MODEL_MANIFEST),
    LEGACY_WHISPER: dict(name='Whisper large-v3-turbo', backend=WHISPER_BACKEND, precision='MLX Safetensors · FP16',
                         languages=('en', 'zh', 'ja', 'ru', 'ko', 'es', 'pt'), modes=tuple(MODES),
                         manifest='whisper-large-v3-turbo.model.json', pin=TRUSTED_MANIFESTS['speech.model']),
    'qwen3-asr-0.6b': dict(name='Qwen3-ASR 0.6B', backend=QWEN_BACKEND, precision='官方 Safetensors · BF16',
                          languages=('en',), modes=('speech.continuous_asr', 'speech.file_asr'),
                          manifest='qwen3-asr-0.6b.model.json',
                          pin='56eed5dcc5c6547776df4036b1b44f42bcadacf2b840b3b722713cbaac83a933'),
    'qwen3-asr-1.7b': dict(name='Qwen3-ASR 1.7B', backend=QWEN_BACKEND, precision='官方 Safetensors · BF16',
                          languages=('en',), modes=('speech.continuous_asr', 'speech.file_asr'),
                          manifest='qwen3-asr-1.7b.model.json',
                          pin='34067bd9c9ecbf4c752a9cfc8e72b3c1f67ed0268f9de87ee52c45a4071526c3'),
}


def preset(variant):
    if variant not in PRESETS:
        raise ResourceError('audio_binding', '请选择语音模型预设。')
    return PRESETS[variant]


def model_spec(variant, root):
    import json
    row = preset(variant)
    path = Path(__file__).with_name(row['manifest'])
    raw = safe_read(path, 16384)
    if sha(raw) != row['pin']:
        raise ResourceError('resource_corrupt', '内置语音模型清单校验失败。')
    model = json.loads(raw)
    return dict(resource_id='speech.model', role=row['name'], version=model['revision'],
                platform='macos-arm64', architecture='arm64', root=str(root),
                origin='https://huggingface.co/' + model['repo'], license='Apache-2.0',
                ownership='external_readonly', distribution_status='user-import', link_policy='deny',
                manifest=dict(path=str(path), bytes=len(raw), sha256=row['pin'], format='model-v0'))


def speech_metadata(variant, specs):
    row = preset(variant)
    model = specs['speech.model']
    files, _, _ = manifest_entries(model)
    weights = {name: entry['sha256'] for name, entry in files.items() if name.endswith('.safetensors')}
    qwen = row['backend'] == QWEN_BACKEND
    return dict(model=variant, revision=model['version'], weights=weights, backend=row['backend'],
                backend_version=WLK_SOURCE, runtime_version=QWEN_RUNTIME if qwen else '0.2.10-r5',
                timestamp_source='pcm-window-word-estimate' if qwen else 'whisper-alignment')


def validate_speech_metadata(value, capability, resources):
    row = preset(value.get('model'))
    if (set(value) != {'model', 'revision', 'weights', 'backend', 'backend_version', 'runtime_version', 'timestamp_source'} or
            capability not in row['modes'] or value['backend'] != row['backend'] or value['backend_version'] != WLK_SOURCE or
            value['runtime_version'] != (QWEN_RUNTIME if row['backend'] == QWEN_BACKEND else '0.2.10-r5') or
            value['timestamp_source'] != ('pcm-window-word-estimate' if row['backend'] == QWEN_BACKEND else 'whisper-alignment') or
            resources.get('speech.model', {}).get('manifest', {}).get('sha256') != row['pin'] or
            not isinstance(value['revision'], str) or len(value['revision']) != 40 or
            not isinstance(value['weights'], dict) or not 1 <= len(value['weights']) <= 2):
        raise ResourceError('audio_binding', '语音任务的模型或后端身份无效。')
    for name, digest in value['weights'].items():
        if '/' in name or not name.endswith('.safetensors'):
            raise ResourceError('audio_binding', '语音任务的权重文件名无效。')
        hash_value(digest)
