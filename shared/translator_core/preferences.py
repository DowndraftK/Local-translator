"""Worker-authoritative defaults with atomic, conflict-aware confirmation."""
import hashlib
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from . import CoreError
from .contracts import default_recipe
from .languages import valid_direction
from .resources import atomic_json, encoded, json_value, no_links, safe_read, token

CONTRACT = 'translation-preferences-v1'
EXECUTION = 'task-execution-v1'
MODELS = {
    'hy-mt2:7b-q8': '202e0ccbae412a107d24c32bc08d7674c579c459a5942e7ed42e5fff631630d9',
    'hy-mt2:1.8b-q8': 'f5ee2ebab8a605d7790eee41a0a34bbc98b4b12792ad7cc9f216dd4949fd7ed1',
}
DEFAULTS = dict(direction='en-zh', translation_model='hy-mt2:7b-q8',
                translation_backend='external_ollama', endpoint='http://127.0.0.1:1',
                speech_backend='wlk_mlx_mps')


def validate_values(values):
    from .translation_routes import AUTO_MODEL, valid_model
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise CoreError('偏好字段不完整或含未知字段。', 'preferences_invalid')
    if (not valid_direction(values['direction']) or
            not (values['translation_model'] == AUTO_MODEL or valid_model(values['translation_model'])) or
            values['translation_backend'] not in ('external_ollama', 'application_ollama') or
            values['speech_backend'] != 'wlk_mlx_mps'):
        raise CoreError('当前方向、模型或后端不在已支持范围。', 'preferences_invalid')
    endpoint = values['endpoint']
    if not isinstance(endpoint, str) or len(endpoint.encode()) > 256:
        raise CoreError('翻译端点无效。', 'preferences_invalid')
    try:
        url = urlsplit(endpoint)
        valid = (url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1') and
                 url.port is not None and 1 <= url.port <= 65535 and not url.username and
                 not url.password and url.path in ('', '/') and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise CoreError('请选择明确端口的本机 HTTP 翻译端点。', 'preferences_invalid')
    return deepcopy(values)


def recipe_for(values, direction=None):
    values = validate_values(values)
    if values['translation_model'] not in MODELS:
        raise CoreError('请通过本机 Ollama 核验所选翻译模型。', 'model_mismatch')
    result = default_recipe(direction or values['direction'])
    result.update(model=values['translation_model'], modelDigest=MODELS[values['translation_model']])
    return result


def execution_for(values, revision):
    values = validate_values(values)
    return dict(version=EXECUTION, preferences_revision=revision,
                translation_backend=values['translation_backend'], endpoint=values['endpoint'],
                speech_backend=values['speech_backend'])


def validate_execution(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'preferences_revision',
            'translation_backend', 'endpoint', 'speech_backend'} or value['version'] != EXECUTION or
            type(value['preferences_revision']) is not int or not 0 <= value['preferences_revision'] < 2**53):
        raise CoreError('任务执行配置版本不受支持，已保存内容可继续回看。', 'execution_incompatible')
    validate_values(dict(DEFAULTS, **{key: value[key] for key in
                    ('translation_backend', 'endpoint', 'speech_backend')}))
    return value


class Preferences:
    def __init__(self, data_root, *, defaults=None, external_only=False):
        self.path = no_links(Path(data_root).absolute()) / 'preferences/translation.json'
        self.external_only = external_only
        self.defaults = validate_values(defaults if defaults is not None else DEFAULTS)
        self.values = deepcopy(self.defaults)
        self.saved = deepcopy(self.defaults)
        self.revision = self.generation = 0
        self.dirty = self.blocked = False
        self.warning = None
        self.baseline = self.baseline_token = self.unconfirmed = None
        self.reload()

    def disk(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None, None
        raw = safe_read(self.path, 16384)
        return raw, token(self.path)

    def reload(self):
        try:
            raw, disk_token = self.disk()
            values, revision = deepcopy(self.defaults), 0
            if raw is not None:
                data = json_value(raw)
                if not isinstance(data, dict):
                    raise CoreError('偏好文件格式损坏。', 'preferences_corrupt')
                if data.get('schema') == 1 and 'version' not in data:
                    if set(data) - {'schema', 'revision', *DEFAULTS}:
                        raise CoreError('旧偏好含不支持字段。', 'preferences_invalid')
                    values = dict(DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS})
                    revision = data.get('revision', 0)
                elif set(data) == {'version', 'revision', 'values'} and data['version'] == CONTRACT:
                    values = data['values']
                    revision = data['revision']
                else:
                    raise CoreError('偏好文件版本不受支持，原文件已保留。', 'preferences_incompatible')
                validate_values(values)
                if self.external_only and values['translation_backend'] == 'application_ollama':
                    endpoint = values['endpoint']
                    if endpoint == DEFAULTS['endpoint']:
                        endpoint = self.defaults['endpoint']
                    values = dict(values, translation_backend='external_ollama', endpoint=endpoint)
                if type(revision) is not int or not 0 <= revision < 2**53:
                    raise CoreError('偏好修订无效。', 'preferences_invalid')
            self.values = deepcopy(values)
            self.saved = deepcopy(values)
            self.revision = revision
            self.baseline, self.baseline_token = raw, disk_token
            self.dirty = self.blocked = False
            self.warning = self.unconfirmed = None
            self.generation += 1
        except (OSError, CoreError, TypeError, KeyError) as exc:
            self.blocked = True
            self.warning = str(exc)
        return self.describe()

    def stage(self, values):
        if self.blocked or self.unconfirmed is not None:
            raise CoreError('请先重新读取或确认当前偏好文件。', 'preferences_unconfirmed')
        values = validate_values(values)
        if self.external_only and values['translation_backend'] != 'external_ollama':
            raise CoreError('请使用已安装的本机 Ollama 服务。', 'preferences_invalid')
        self.values = values
        self.dirty = self.values != self.saved or self.baseline is None
        self.generation += 1
        self.warning = None
        return self.describe()

    def discard(self):
        if self.unconfirmed is not None:
            raise CoreError('写入结果尚待确认，请先核对磁盘。', 'preferences_unconfirmed')
        self.values = deepcopy(self.saved)
        self.dirty = False
        self.generation += 1
        self.warning = None
        return self.describe()

    def save(self):
        if self.blocked or self.unconfirmed is not None:
            raise CoreError('偏好保存尚待核对，请使用重新读取或确认入口。', 'preferences_unconfirmed')
        if self.disk() != (self.baseline, self.baseline_token):
            self.warning = '偏好文件已被外部修改，请重新读取后核对。'
            raise CoreError(self.warning, 'preferences_changed')
        value = dict(version=CONTRACT, revision=self.revision + 1, values=validate_values(self.values))
        raw = encoded(value)
        try:
            atomic_json(self.path, value)
            actual, actual_token = self.disk()
            if actual != raw:
                raise CoreError('偏好写后回读未确认。', 'preferences_unconfirmed')
        except (OSError, CoreError) as exc:
            # An atomic replacement can succeed before sync/readback fails.
            # Record the exact intended bytes and require explicit confirmation.
            try:
                actual, _ = self.disk()
            except (OSError, CoreError):
                actual = None
            if actual == raw or actual is None and self.path.exists():
                self.unconfirmed = value
            self.dirty = True
            self.warning = '偏好保存未确认，请保留本页并核对。' + str(exc)
            raise CoreError(self.warning, 'preferences_save_failed') from exc
        self._accept(value, raw, actual_token)
        return self.describe()

    def _accept(self, value, raw, disk_token):
        self.revision = value['revision']
        self.saved = deepcopy(value['values'])
        self.values = deepcopy(self.saved)
        self.baseline, self.baseline_token = raw, disk_token
        self.dirty = self.blocked = False
        self.unconfirmed = self.warning = None
        self.generation += 1

    def confirm(self):
        if self.unconfirmed is None:
            raise CoreError('当前没有待确认的偏好写入。', 'preferences_unconfirmed')
        raw, disk_token = self.disk()
        if raw != encoded(self.unconfirmed):
            raise CoreError('磁盘内容与本次待确认写入不符，请保留文件后重新读取。', 'preferences_changed')
        self._accept(self.unconfirmed, raw, disk_token)
        return self.describe()

    def require_confirmed(self):
        if self.dirty or self.blocked or self.unconfirmed is not None:
            raise CoreError('请先保存、放弃或核对偏好，再创建新任务。', 'preferences_unconfirmed')
        if self.disk() != (self.baseline, self.baseline_token):
            raise CoreError('偏好文件已在外部改变，请重新读取。', 'preferences_changed')
        return self.saved

    def describe(self):
        return dict(version=CONTRACT, values=deepcopy(self.values), saved_values=deepcopy(self.saved),
                    revision=self.revision, generation=self.generation, dirty=self.dirty,
                    blocked=self.blocked, unconfirmed=self.unconfirmed is not None, warning=self.warning,
                    path=str(self.path), disk_sha256=hashlib.sha256(self.baseline).hexdigest()
                    if self.baseline is not None else None,
                    supported=dict(directions=['en-zh', 'zh-en'], models=list(MODELS),
                                   translation_backends=['external_ollama', 'application_ollama'],
                                   speech_backends=['wlk_mlx_mps'], audio_direction='en-zh'))


def validate_description(value):
    keys = {'version', 'values', 'saved_values', 'revision', 'generation', 'dirty', 'blocked',
            'unconfirmed', 'warning', 'path', 'disk_sha256', 'supported'}
    if not isinstance(value, dict) or set(value) != keys or value['version'] != CONTRACT:
        raise CoreError('偏好响应版本或字段无效。', 'preferences_incompatible')
    validate_values(value['values'])
    validate_values(value['saved_values'])
    supported = dict(directions=['en-zh', 'zh-en'], models=list(MODELS),
                     translation_backends=['external_ollama', 'application_ollama'],
                     speech_backends=['wlk_mlx_mps'], audio_direction='en-zh')
    if (any(type(value[k]) is not int or not 0 <= value[k] < 2**53 for k in ('revision', 'generation')) or
            any(type(value[k]) is not bool for k in ('dirty', 'blocked', 'unconfirmed')) or
            value['warning'] is not None and (not isinstance(value['warning'], str) or len(value['warning'].encode()) > 4096) or
            not isinstance(value['path'], str) or len(value['path'].encode()) > 4096 or
            not Path(value['path']).is_absolute() or value['supported'] != supported or
            value['disk_sha256'] is not None and not re.fullmatch('[0-9a-f]{64}', value['disk_sha256']) or
            len(encoded(value)) > 16384):
        raise CoreError('偏好响应内容无效。', 'preferences_invalid')
    return value
