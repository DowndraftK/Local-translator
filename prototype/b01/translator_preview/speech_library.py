"""Managed local imports and per-model inference receipts for the Mac."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from translator_core import CoreError, Cancelled
from translator_core.local_environment import publish_import, regular_size
from translator_core.qwen_runtime import spec_at, verify_runtime
from translator_core.resource_lifecycle import copy_manifest_resource, copy_verified_file
from translator_core.resource_macos import boot_identity, trusted_manifest
from translator_core.resources import (BINDING, CHECK_SECONDS, EXECUTION, REQUIRED, SPEECH_BINDING, atomic_json,
    content_identity, json_value, manifest_entries, no_links, parse_binding, safe_read, stop_check, tokens_match,
    verify_resource)
from translator_core.speech_presets import MODES, PRESETS, QWEN_BACKEND, QWEN_RUNTIME, model_spec, preset, speech_metadata
from translator_core.store import atomic_write

from .paths import in_app, package_speech_variants

STATE_VERSION = 'speech-presets-v1'
RECEIPT_VERSION = 'speech-inference-check-v1'


def checked_json(path, limit=65536):
    try:
        return json_value(safe_read(path, limit))
    except FileNotFoundError:
        return None


def files_state(spec):
    root = Path(spec['root'])
    files, _, _ = manifest_entries(spec)
    missing = [name for name in files if not (root/name).exists()]
    invalid = [name for name, row in files.items() if name not in missing and
               not regular_size(root/name, row['size'])]
    return missing, invalid


def validate_qwen_weights(root, names):
    tensors = {}
    bf16 = False
    for name in names:
        with open(root/name, 'rb') as stream:
            length = int.from_bytes(stream.read(8), 'little')
            if not 0 < length <= 4*1024**2:
                raise CoreError(f'Safetensors 文件头无效 {name}。', 'weight_format')
            header = json_value(stream.read(length))
        for key, value in header.items():
            if key != '__metadata__':
                if key in tensors:
                    raise CoreError(f'权重分片重复包含张量 {key}。', 'weight_format')
                tensors[key] = name
                bf16 |= value['dtype'] == 'BF16'
    if not bf16:
        raise CoreError('权重未包含 BF16 张量。', 'weight_format')
    if len(names) > 1:
        index = json_value(safe_read(root/'model.safetensors.index.json'))
        if index.get('weight_map') != tensors:
            raise CoreError('model.safetensors.index.json 与实际分片张量不一致。', 'weight_format')


class SpeechLibrary:
    def __init__(self, package, data_root, audio):
        self.package, self.data_root, self.audio = package, data_root, audio
        self.root = data_root/'models/speech-presets'
        self.state_path = data_root/'resources/speech-presets.json'
        self.check_root = data_root/'resources/speech-checks'
        self.baseline = None
        self.warning = None
        self.state = dict(version=STATE_VERSION, roots={}, choices={})
        self.common_evidence = {}
        try:
            loaded = checked_json(self.state_path)
            if loaded is not None:
                if (loaded.get('version') != STATE_VERSION or set(loaded) != set(self.state)
                        or not isinstance(loaded['roots'], dict) or not isinstance(loaded['choices'], dict)):
                    raise CoreError('保存的语音预设配置无法识别。', 'speech_selection')
                self.state = self.baseline = loaded
        except (OSError, CoreError, AttributeError) as exc:
            self.warning = f'保存的语音预设配置无法读取，原文件保留。{exc}'

    def commit(self, *, roots=None, choices=None):
        if self.warning or checked_json(self.state_path) != self.baseline:
            raise CoreError(self.warning or '语音预设配置已改变，请重新打开 App。', 'speech_selection')
        value = dict(self.state)
        if roots is not None:
            value['roots'] = roots
        if choices is not None:
            value['choices'] = choices
        if value != self.baseline:
            atomic_json(self.state_path, value)
            self.state = self.baseline = value

    def select(self, mode, variant):
        if mode not in ('live', 'file'):
            raise CoreError('语音模型用途无效。', 'speech_selection')
        preset(variant)
        self.commit(choices=dict(self.state['choices'], **{mode: variant}))

    def imported(self, key):
        value = self.state['roots'].get(key)
        if not value:
            return None
        name = value['directory']
        if not isinstance(name, str) or Path(name).name != name or not name.startswith(key+'-'):
            raise CoreError('已登记的语音目录超出托管范围。', 'unsafe_path')
        return no_links(self.root/name)

    def specs(self, variant, defaults, saved):
        row = preset(variant)
        base = {spec['resource_id']: spec for spec in defaults['resources']}
        model = self.imported(variant)
        if row['backend'] == QWEN_BACKEND:
            specs = {key: base[key] for key in ('speech.runtime', 'speech.backend', 'speech.bridge')}
            specs['speech.model'] = model_spec(variant, model or self.root/variant)
            runtime = self.imported('qwen-runtime')
            if runtime:
                spec = spec_at(runtime)
                if spec['manifest']['sha256'] != self.state['roots']['qwen-runtime'].get('sha256'):
                    raise CoreError('Qwen 运行包清单发生改变，请重新导入运行组件。', 'runtime_version')
                specs['speech.qwen-runtime'] = spec
            return specs
        legacy = package_speech_variants(self.package, self.data_root, defaults, saved, external_only=True)[variant]
        specs = {key: base[key] for key in ('speech.runtime', 'speech.backend', 'speech.bridge', 'speech.assets')}
        # Keep older compatible explicit imports, including their common asset directory.
        previous_assets = next((spec for spec in (saved or {}).get('resources', [])
                                if spec['resource_id'] == 'speech.assets' and not in_app(spec['root'])
                                and spec['manifest']['sha256'] == base['speech.assets']['manifest']['sha256']), None)
        assets = self.imported(variant+'-assets')
        asset_root = assets or (previous_assets or base['speech.assets'])['root']
        specs['speech.assets'] = dict(base['speech.assets'], root=str(asset_root))
        specs['speech.model'] = dict(legacy, root=str(model or legacy['root']))
        return specs

    def report(self, defaults, saved):
        rows = {}
        memo = {}
        for variant, row in PRESETS.items():
            try:
                specs = self.specs(variant, defaults, saved)
                model = specs['speech.model']
                missing_model, invalid_model = files_state(model)
                assets = specs.get('speech.assets')
                missing_assets, invalid_assets = files_state(assets) if assets else ([], [])
                runtime_missing = row['backend'] == QWEN_BACKEND and 'speech.qwen-runtime' not in specs
                if row['backend'] != QWEN_BACKEND:
                    runtime_missing = not Path(specs['speech.runtime']['root'], 'Python/bin/python3.12').is_file()
                facts = None
                if not (missing_model or invalid_model or missing_assets or invalid_assets or runtime_missing):
                    facts = self.current_check(variant, specs, memo)
                installed = not (missing_model or invalid_model or missing_assets or invalid_assets or runtime_missing)
                modes = {mode: '本版未开放' if mode not in row['modes'] else
                         '可用' if facts and facts['modes'].get(mode, {}).get('passed') is True else
                         '检查未通过' if facts and mode in facts['modes'] else
                         '待验证' if installed else '待导入' for mode in MODES}
                rows[variant] = dict(revision=model['version'], source=model['origin'],
                    model=str(Path(model['root'])), missing_model=missing_model, invalid_model=invalid_model,
                    missing_assets=missing_assets, invalid_assets=invalid_assets, runtime_missing=runtime_missing,
                    modes=modes, details={key: value.get('message') for key, value in (facts or {}).get('modes', {}).items()})
            except (OSError, CoreError, KeyError, TypeError, ValueError) as exc:
                rows[variant] = dict(error=str(exc), modes={mode: '待检查' for mode in MODES})
        return dict(rows=rows, choices=self.state['choices'], warning=self.warning)

    def stage_import(self, variant, kind, path, defaults, saved, cancel, progress):
        source = no_links(path)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        deadline = time.monotonic() + CHECK_SECONDS
        key = 'qwen-runtime' if kind == 'runtime' else variant
        if kind == 'runtime':
            spec = spec_at(source)
        else:
            specs = self.specs(variant, defaults, saved)
            qwen = preset(variant)['backend'] == QWEN_BACKEND
            if kind == 'assets' and not qwen:
                key += '-assets'
                spec = specs['speech.assets']
            else:
                spec = specs['speech.model']
        entries, _, _ = manifest_entries(spec)
        with tempfile.TemporaryDirectory(prefix='.import-', dir=self.root) as tmp:
            payload = Path(tmp)/'payload'
            if kind == 'runtime':
                copy_manifest_resource(spec, payload, cancel, deadline, lambda _, __, n, total: progress(n, total))
                verify_runtime(payload, spec['manifest']['sha256'], cancel, lambda *_: None)
            else:
                self.copy_import(payload, source, spec, entries, kind, qwen, cancel, deadline, progress)
            directory = publish_import(payload, self.root/f'{key}-{uuid.uuid4().hex}', cancel)
        record = {'directory': directory.name}
        if kind == 'runtime':
            record['sha256'] = spec['manifest']['sha256']
        return key, record

    def copy_import(self, payload, source, spec, entries, kind, qwen, cancel, deadline, progress):
        if kind == 'model' and not source.is_dir():
            raise CoreError('请选择完整模型目录。', 'speech_import')
        required_model = {'config.json'} | {n for n in entries if '.safetensors' in n}
        primary = required_model if kind == 'model' else set()
        allowed = set(entries) if kind == 'model' or not qwen else set(entries)-required_model
        old_root = Path(spec['root'])
        if source.is_file():
            provided = {name: source for name in allowed if Path(name).name == source.name}
        else:
            provided = {name: source/name for name in allowed if (source/name).exists() or (source/name).is_symlink()}
            if kind == 'assets' and not qwen:
                for name in allowed-provided.keys():
                    flat = source/Path(name).name
                    if flat.exists() or flat.is_symlink():
                        provided[name] = flat
        if primary - provided.keys():
            raise CoreError('目录与所选预设不符或缺少文件。需要 ' + '、'.join(sorted(primary-provided.keys())) + '。', 'speech_import')
        if not provided:
            raise CoreError('选择的文件不属于该预设。可导入 ' + '、'.join(sorted(allowed)) + '。', 'speech_import')
        sources = dict(provided)
        for name, entry in entries.items():
            candidate = old_root/name
            if name not in sources and name not in primary and regular_size(candidate, entry['size']):
                sources[name] = candidate
        total = sum(entries[name]['size'] for name in sources)
        if shutil.disk_usage(payload.parent).free < total+64*1024**2:
            raise CoreError('临时复制空间不足。', 'insufficient_space')
        payload.mkdir(mode=0o700)
        copied = 0
        for name, path in sources.items():
            try:
                copy_verified_file(path, payload/name, entries[name], cancel, deadline,
                                   lambda n: progress(copied+n, total))
            except CoreError as exc:
                if exc.code in ('check_cancelled', 'check_timeout'):
                    raise
                raise CoreError(f'文件 {name} 校验失败。{exc}', exc.code) from exc
            copied += entries[name]['size']
        if sources.keys() >= entries.keys():
            verify_resource(dict(spec, root=str(payload)), cancel, deadline, lambda *_: None)
        weights = sorted(name for name in required_model if name.endswith('.safetensors'))
        if qwen and required_model <= sources.keys():
            validate_qwen_weights(payload, weights)

    def current_check(self, variant, specs, memo=None):
        memo = {} if memo is None else memo
        try:
            record = checked_json(self.check_root/f'{variant}.json', 32*1024**2)
            if (not record or record['version'] != RECEIPT_VERSION or record['boot'] != boot_identity() or
                    record['adapter'] != self.audio.identity or set(record['files']) != set(specs)):
                return None
            receipt = record['receipt']
            name = receipt['path']
            if Path(name).name != name or not name.startswith('check-probe-'):
                return None
            raw = safe_read(self.check_root/name, 32*1024**2)
            if (len(raw) != receipt['bytes'] or hashlib.sha256(raw).hexdigest() != receipt['sha256'] or
                    json_value(raw) != {key: value for key, value in record.items() if key != 'receipt'}):
                return None
            for name, spec in specs.items():
                evidence = record['files'][name]
                key = (name, spec['root'], spec['manifest']['sha256'])
                if key not in memo:
                    memo[key] = tokens_match(spec, evidence)
                if not memo[key]:
                    return None
            return record
        except (CoreError, OSError, KeyError, TypeError, ValueError):
            return None

    def binding(self, variant, language, capability, specs, record):
        wanted = REQUIRED[capability] if capability == 'audio.decode' else specs
        bound = {key: dict(identity=content_identity(specs[key]), root=specs[key]['root'],
                           manifest=specs[key]['manifest']) for key in wanted}
        check = record['receipt']
        data = dict(resources=bound, capability=capability, check=check, checked_at=record['checked_at'])
        if capability == 'audio.decode':
            return parse_binding(dict(data, version=BINDING, execution=EXECUTION, prerequisite_only=True,
                                      model_load='not_tested', inference='not_tested'))
        row = preset(variant)
        execution = dict(EXECUTION, language=language) if row['backend'] != QWEN_BACKEND else dict(
            language=language, backend=QWEN_BACKEND, runtime=QWEN_RUNTIME)
        return parse_binding(dict(data, version=SPEECH_BINDING, execution=execution, prerequisite_only=False,
                                 model_load='verified', inference='verified', speech=speech_metadata(variant, specs)))

    def verify(self, variant, specs, cancel, report):
        from translator_core.speech_probe import probe_modes
        from translator_core.resources import encoded

        row = preset(variant)
        if row['backend'] == QWEN_BACKEND and 'speech.qwen-runtime' not in specs:
            raise CoreError(f'请先导入 Qwen 运行包 {QWEN_RUNTIME}。', 'runtime_version')
        deadline = time.monotonic()+CHECK_SECONDS
        evidence = {}
        for name, spec in specs.items():
            stop_check(cancel, deadline)
            if name in ('speech.runtime', 'speech.bridge', 'speech.backend', 'speech.assets', 'speech.model'):
                if name != 'speech.model' or row['backend'] != QWEN_BACKEND:
                    if not trusted_manifest(name, spec['manifest']['sha256']):
                        raise CoreError(f'组件 {name} 版本不匹配。', 'speech_selection')
            missing, invalid = files_state(spec) if name in ('speech.model', 'speech.assets') else ([], [])
            if missing or invalid:
                raise CoreError('请在该预设补齐或重新导入 ' + '、'.join(missing+invalid) + '。', 'speech_import')
            report(f'正在核对 {spec["role"]} 的文件…')
            key = (name, spec['root'], spec['manifest']['sha256'])
            previous = self.common_evidence.get(key)
            if previous and tokens_match(spec, previous):
                evidence[name] = previous
            elif name == 'speech.qwen-runtime':
                _, evidence[name] = verify_runtime(Path(spec['root']), spec['manifest']['sha256'], cancel, lambda *_: None)
            else:
                evidence[name] = verify_resource(spec, cancel, deadline, lambda *_: None)
            if name in ('speech.runtime', 'speech.bridge', 'speech.backend', 'speech.qwen-runtime'):
                self.common_evidence[key] = evidence[name]
        if row['backend'] == QWEN_BACKEND:
            weights = [name for name in manifest_entries(specs['speech.model'])[0] if name.endswith('.safetensors')]
            validate_qwen_weights(Path(specs['speech.model']['root']), weights)
        work = no_links(self.check_root)/f'probe-{uuid.uuid4().hex}'
        work.mkdir(parents=True, mode=0o700)
        checked_at = time.time()
        pending = dict(path='check-'+work.name+'.json', bytes=1, sha256='0'*64)
        self.audio.close()
        try:
            modes = probe_modes(self.audio, work, row['modes'], cancel, report,
                                lambda mode: self.binding(variant, 'en', mode, specs,
                                                          dict(checked_at=checked_at, receipt=pending)))
            if cancel.is_set():
                raise Cancelled()
        finally:
            self.audio.close()
        for name, spec in specs.items():
            if not tokens_match(spec, evidence[name]):
                raise CoreError(f'检查时资源 {spec["role"]} 发生变化，请重新检查。', 'resource_changed')
        record = dict(version=RECEIPT_VERSION, checked_at=checked_at, boot=boot_identity(), adapter=self.audio.identity,
                      files=evidence, modes=modes)
        raw = encoded(record)
        self.check_root.mkdir(parents=True, exist_ok=True)
        atomic_write(raw, self.check_root/pending['path'])
        return dict(record, receipt=dict(path=pending['path'], bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
