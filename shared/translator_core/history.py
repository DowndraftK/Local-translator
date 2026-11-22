"""Versioned sidecar metadata and bounded, model-free history queries.

Task bodies are never rewritten here. There is no second task database or index.
"""
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
import unicodedata
from copy import deepcopy
from pathlib import Path

from . import CoreError, Cancelled
from .contracts import sha, strict_json, task_id
from .store import safe_file, is_link
from .text_rules import measure_text

METADATA_VERSION = 'task-history-metadata-v1'
QUERY_VERSION = 'task-history-query-v2'
LOCATOR_VERSION = 'task-history-locator-v1'
MAX_HITS = 3
MAX_LOCATOR_BYTES = 4096
MAX_METADATA_BYTES = 16384
MAX_QUERY_CHARS = 512
MAX_QUERY_BYTES = 2048
MAX_PAGE = 20
MAX_PAGE_BYTES = 49152
MAX_SCAN_TASKS = 512
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_SCAN_SECONDS = 2
FOUNDATION_EPOCH = 978307200
KINDS = ('文字', '文档', '图片', '音频')
STATUSES = ('draft', 'preparing', 'running', 'completed', 'stopped', 'partial', 'failed', 'review')


def source_identity(task):
    """Saved provenance only. Searching never reads or decodes media assets."""
    kind = task['kind']
    asset = task.get('asset') or {}
    if kind == '文字':
        return dict(id=task['id'], fingerprint=asset.get('fingerprint'), name=task['name'])
    if kind in ('文档', '图片'):
        from .images import content
        manifest = content(task)
        source = manifest.get('source') or manifest
        return dict(id=source['id'], fingerprint=manifest['fingerprint'], name=manifest['file'])
    audio = task['audio']
    recording = audio.get('recording')
    source = recording['source'] if recording else audio['source']
    name = audio['source'].get('file', task['name'])
    if recording:
        origin = '麦克风录音' if source['kind'] == 'microphone' else '电脑声音录音'
        name = origin + ' · ' + source.get('deviceName', source.get('name', name))
    return dict(id=source['id'], fingerprint=asset.get('fingerprint'), name=name,
                recording_session=recording['session'] if recording else None)


def content_revision(task):
    if task['kind'] == '文字':
        return sha(task['input'].encode())
    if task['kind'] in ('文档', '图片'):
        from .images import content, units
        revisions = [(p['id'], p['revision']) for p in units(content(task))]
    else:
        revisions = [(c['id'], c['revision']) for c in task['audio']['cues']]
    return sha(encoded(revisions))


def searchable_fields(task, title):
    """Yield independent effective fields with their stable navigation unit."""
    yield '标题', title, dict(unit_type='task')
    yield '原任务名', task['name'], dict(unit_type='task')
    if task['kind'] != '文字':
        yield '来源', source_identity(task)['name'], dict(unit_type='task')
    parts = (task.get('text') or {}).get('segments', [])
    if task['kind'] == '文字':
        yield '原文', task['input'], dict(unit_type='task')
        translated = '\n\n'.join(p['result']['translation'] for p in parts if p['state'] == 'completed')
        yield '译文', translated, dict(unit_type='task')
    elif task['kind'] in ('文档', '图片'):
        from .images import content, units, unit_field
        manifest = content(task)
        field = unit_field(manifest)
        for unit in units(manifest):
            position = dict(unit_type=field, unit_id=unit['id'], unit_revision=unit['revision'])
            yield '原文', unit['text'], position
            translated = []
            for mapping in manifest['sources']:
                if mapping[field] == unit['id'] and mapping['revision'] == unit['revision'] and parts:
                    part = parts[mapping['segmentID']]
                    if part['state'] == 'completed':
                        translated.append(part['result']['translation'])
            yield '译文', '\n\n'.join(translated), position
    else:
        for cue in task['audio']['cues']:
            position = dict(unit_type='cue', unit_id=cue['id'], unit_revision=cue['revision'],
                start_sample=cue['startSample'], end_sample=cue['endSample'], rate=16000)
            yield '原文', cue['source'], position
            if cue['state'] == 'completed' and cue.get('translationRevision') == cue['revision']:
                yield '译文', cue['result']['translation'], position


def scale_label(task, completed, total):
    if task['kind'] == '文字':
        return f'{completed}/{total} 段'
    if task['kind'] in ('文档', '图片'):
        from .images import content, units
        pages = units(content(task))
        obtained = sum(bool(p['text']) for p in pages)
        review = sum(not p['reviewed'] for p in pages)
        unit = '图片单元' if task['kind'] == '图片' else '页'
        return f'{obtained}/{len(pages)} {unit}已有正文 · {review} 待核对 · 已译{completed}/{total}段'
    audio = task['audio']
    samples = (audio.get('pcm') or {}).get('samples', 0)
    recording = audio.get('recording')
    if recording:
        samples = recording['watermarks']['durable']
    seconds = samples // 16000
    return f'{seconds//60:02}:{seconds%60:02} · {total} 条字幕 · 已译{completed}条'


def encoded(value, *, canonical=False):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False, sort_keys=canonical).encode()


def search_view(text):
    text = unicodedata.normalize('NFC', unicodedata.normalize('NFC', text).casefold())
    return ' '.join(text.replace('\r\n', '\n').replace('\r', '\n').split())


def display_title(value):
    if value is None: return None
    if not isinstance(value, str): raise CoreError('请输入显示标题。', 'invalid_title')
    # Reject controls before trimming, so an embedded or trailing newline is not hidden.
    if any(unicodedata.category(c) in ('Cc', 'Cs', 'Zl', 'Zp') or
           unicodedata.category(c) == 'Cf' and c not in ('\u200c', '\u200d') for c in value):
        raise CoreError('标题不能包含换行、制表或控制字符。', 'invalid_title')
    value = unicodedata.normalize('NFC', value.strip())
    if len(value) > 120 or len(value.encode()) > 512:
        raise CoreError('标题最多120个字符，且不超过512字节；请缩短后保存。', 'invalid_title')
    return value or None


def business_status(task):
    if task["kind"] == "音频":
        audio = task["audio"]
        capture = audio.get('recording')
        if capture and capture['state'] in ('preparing', 'waiting_permission', 'starting', 'recording', 'pausing', 'stopping', 'finalizing'):
            return 'running'
        if capture and capture['state'] in ('paused', 'interrupted', 'save_blocked'):
            return 'stopped'
        if audio["phase"] in ("decoding", "recognizing", "refining"): return "running"
        if audio["phase"] in ("failed", "stopped"): return audio["phase"]
        if not audio["asrComplete"]: return "draft"
        cues = audio["cues"]
        return "completed" if cues and all(c["state"] == "completed" for c in cues) else "partial" if any(c["state"] in ("completed", "failed", "stopped") for c in cues) else "review"
    job = task.get('text') or {}
    phase = job.get('phase', (task.get('document') or {}).get('phase', 'draft'))
    if task['kind'] in ('文档','图片'):
        from .images import content, units
        pages = units(content(task))
        if not job:
            if any(p['state'] == 'recognizing' for p in pages): return 'preparing'
            if any(p['state'] == 'stopped' for p in pages): return 'stopped'
            if any(p['state'] == 'failed' for p in pages): return 'partial'
            return 'draft' if any(p['state'] == 'pending' for p in pages) else 'review'
        if phase == 'completed' and any(p['state'] != 'extracted' for p in pages): return 'partial'
    elif task['kind'] != '文字': return phase
    parts = job.get('segments', [])
    count = sum(p['state'] == 'completed' for p in parts)
    if phase == 'completed' and (not parts or count != len(parts)):
        return 'partial' if count else 'draft'
    if phase not in STATUSES:
        return 'review' if task['kind'] in ('文档','图片') else 'failed'
    return phase


class HistoryRepository:
    def __init__(self, store, session_id=None):
        self.store, self.session_id = store, session_id
        self.root_id = sha(str(store.root.resolve()).encode())
        self.secret = secrets.token_bytes(32)
        self.metadata_baselines = {}
        self.task_baselines = {}

    def description(self):
        return dict(contract=QUERY_VERSION, metadata=METADATA_VERSION, locator=LOCATOR_VERSION,
                    managed_kinds=list(KINDS), max_hits=MAX_HITS, max_locator_bytes=MAX_LOCATOR_BYTES, root_id=self.root_id,
                    max_query_chars=MAX_QUERY_CHARS, max_query_bytes=MAX_QUERY_BYTES,
                    max_page=MAX_PAGE, max_page_bytes=MAX_PAGE_BYTES,
                    max_scan_tasks=MAX_SCAN_TASKS, max_scan_bytes=MAX_SCAN_BYTES,
                    max_scan_seconds=MAX_SCAN_SECONDS)

    def guard_root(self):
        root = self.store.root
        if self.store.lock.closed or any(is_link(p) for p in (root, *root.parents)):
            raise CoreError('任务根已关闭或包含不安全链接。', 'unsafe_path')
        if sha(str(root.resolve()).encode()) != self.root_id:
            raise CoreError('任务根归属已变化，请重新打开应用。', 'root_mismatch')
    def path(self, identity):
        self.guard_root()
        directory = self.store.directory(identity)
        if not directory.is_dir(): raise CoreError('此任务不存在。', 'task_missing')
        path = directory / '.history.json'
        if is_link(path): raise CoreError('历史信息路径不安全；原文件保留。', 'unsafe_path')
        return path

    def metadata(self, identity, kind, remember=True):
        path = self.path(identity)
        if not path.exists():
            if remember:
                self.metadata_baselines[task_id(identity)] = None
                while len(self.metadata_baselines) > MAX_SCAN_TASKS:
                    self.metadata_baselines.pop(next(iter(self.metadata_baselines)))
            return dict(schema=1, contract=METADATA_VERSION, root_id=self.root_id,
                        task_id=task_id(identity), kind=kind, revision=0, favorite=False,
                        display_title=None, trashed_at=None, updated_at=None)
        try:
            raw = safe_file(path, MAX_METADATA_BYTES)
            data = strict_json(raw)
            if not isinstance(data, dict): raise ValueError('不是对象')
            if type(data.get('schema')) is not int or data['schema'] != 1 or data.get('contract') != METADATA_VERSION:
                raise CoreError('此历史信息版本尚不支持；原文件保留。', 'metadata_version')
            if (data.get('root_id') != self.root_id or data.get('task_id') != task_id(identity) or data.get('kind') != kind):
                raise CoreError('历史信息不属于此任务或任务根；原文件保留。', 'metadata_owner')
            if (type(data.get('revision')) is not int or not 1 <= data['revision'] < 2**63 or
                    type(data.get('favorite')) is not bool or data.get('display_title') != display_title(data.get('display_title'))):
                raise ValueError('标题、收藏或修订字段无效')
            for key in ('updated_at', 'trashed_at'):
                value = data.get(key)
                if value is None and key == 'trashed_at': continue
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError('历史时间字段无效')
            for key in ('favorite','display_title','trashed_at','updated_at'):
                if key not in data: raise ValueError('缺少历史字段')
            if remember:
                self.metadata_baselines[task_id(identity)] = sha(raw)
                while len(self.metadata_baselines) > MAX_SCAN_TASKS:
                    self.metadata_baselines.pop(next(iter(self.metadata_baselines)))
            return data
        except CoreError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise CoreError('此条历史信息无法读取，原始数据已保留：'+str(exc), 'metadata_corrupt') from exc

    def update(self, identity, *, root_id, session_id, task_revision, metadata_revision, changes):
        with self.store.mutex:
            if root_id != self.root_id or session_id != self.session_id:
                raise CoreError('操作归属已变化，请刷新记录后重试。', 'history_owner')
            identity = task_id(identity)
            current_path = self.store.directory(identity) / 'current.json'
            current_hash = sha(safe_file(current_path))
            expected = self.task_baselines.get(identity, current_hash)
            if current_hash != expected:
                raise CoreError('磁盘任务已被替换，请刷新核对后重试。', 'source_changed')
            task, warning = self.store.read(identity)
            if task['kind'] not in KINDS:
                raise CoreError('此类型的历史管理尚不支持。', 'history_kind')
            if warning: raise CoreError('此任务保存或来源受阻，请先保留结果。', 'save_blocked')
            if type(task_revision) is not int or task_revision != task['revision']:
                raise CoreError('任务内容已更新，请刷新后重试。', 'stale_task_revision')
            path = self.path(identity)
            before_hash = sha(safe_file(path, MAX_METADATA_BYTES)) if path.exists() else None
            if before_hash != self.metadata_baselines.get(identity, before_hash):
                raise CoreError('磁盘历史信息已被替换，请刷新核对后重试。', 'metadata_changed')
            previous = self.metadata(identity, task['kind'], remember=False)
            if type(metadata_revision) is not int or previous['revision'] != metadata_revision:
                raise CoreError('记录已更新，请刷新后重试；此次未覆盖。', 'stale_metadata_revision')
            if not isinstance(changes, dict) or not changes or set(changes)-{'favorite','display_title','trashed'}:
                raise CoreError('不支持此历史操作。', 'unsupported_operation')
            current = deepcopy(previous)
            if 'favorite' in changes:
                if type(changes['favorite']) is not bool: raise CoreError('收藏状态无效。', 'invalid_metadata')
                current['favorite'] = changes['favorite']
            if 'display_title' in changes: current['display_title'] = display_title(changes['display_title'])
            now = time.time()
            if 'trashed' in changes:
                if type(changes['trashed']) is not bool: raise CoreError('回收状态无效。', 'invalid_metadata')
                current['trashed_at'] = (previous['trashed_at'] or now) if changes['trashed'] else None
            current['revision'] += 1; current['updated_at'] = now
            raw = encoded(current)
            if len(raw) > MAX_METADATA_BYTES: raise CoreError('历史信息超过容量，原文件保留。', 'metadata_limit')
            path = self.path(identity)
            try:
                actual_hash = sha(safe_file(path, MAX_METADATA_BYTES)) if path.exists() else None
                if actual_hash != before_hash or sha(safe_file(current_path)) != current_hash:
                    raise CoreError('保存前磁盘内容已变化，请刷新核对。', 'source_changed')
                self.store.write(raw, path)
                if safe_file(path, MAX_METADATA_BYTES) != raw: raise OSError('写后核对失败')
            except (OSError, CoreError) as exc:
                # Publication might have succeeded before a directory fsync failed.
                self.store.history_epoch += 1
                raise CoreError('历史修改尚未确认保存，请刷新核对；当前内容已保留。'+str(exc), 'metadata_write_failed') from exc
            self.store.history_epoch += 1
            self.metadata_baselines[identity] = sha(raw)
            return dict(task_id=task['id'], task_revision=task['revision'], metadata=current,
                        epoch=self.store.history_epoch, root_id=self.root_id)

    def summary(self, task, meta, warning=None, query='', cancel=None, deadline=None, epoch=None):
        parts = task["audio"]["cues"] if task["kind"] == "音频" else (task.get('text') or {}).get('segments', [])
        title = meta['display_title'] or task['name']
        source = source_identity(task)
        hits = []
        more_hits = False
        snippet = ''
        for field, value, position in searchable_fields(task, title):
            if cancel and cancel.is_set():
                raise Cancelled()
            if deadline is not None and time.monotonic() > deadline:
                raise CoreError('已达到本次查询时限。', 'history_deadline')
            if not query and field != '原文':
                continue
            view = search_view(value)
            if not query:
                if view:
                    snippet = view[:160]
                    break
                continue
            offset = view.find(query)
            if offset < 0:
                continue
            if len(hits) == MAX_HITS:
                more_hits = True
                break
            fragment = ('…' if offset > 40 else '') + view[max(0, offset-40):offset+120]
            locator = dict(version=LOCATOR_VERSION, root_id=self.root_id, session_id=self.session_id,
                epoch=self.store.history_epoch if epoch is None else epoch, task_id=task['id'],
                task_revision=task['revision'], metadata_revision=meta['revision'], kind=task['kind'],
                field=field, source_id=source['id'], source_fingerprint=source['fingerprint'],
                content_revision=content_revision(task), **position)
            if source.get('recording_session'):
                locator['recording_session'] = source['recording_session']
            locator['signature'] = hmac.new(self.secret, encoded(locator, canonical=True), hashlib.sha256).hexdigest()
            hits.append(dict(field=field, snippet=fragment, locator=locator))
        if query and not hits:
            return None
        if hits:
            snippet = hits[0]['snippet']
        completed = sum(p['state'] == 'completed' for p in parts)
        return dict(id=task['id'], name=title[:120], original_name=task['name'][:120], kind=task['kind'],
                    updated=task['updated'], created=task['created'], revision=task['revision'],
                    phase=business_status(task), direction=task['configuration']['direction'],
                    completed=completed, total=len(parts), scale=scale_label(task, completed, len(parts)),
                    source_name=source['name'][:160], parent=task.get('parent'),
                    recording_state=((task.get('audio') or {}).get('recording') or {}).get('state'),
                    source_ref={k:v for k,v in (task.get('asset') or {}).items() if k in ('filename','byteCount','fingerprint')},
                    source_chars=measure_text(task['input'],task['configuration']['direction']).graphemes if task['kind']=='文字' else 0, warning=(warning or '')[:300] or None,
                    favorite=meta['favorite'], renamed_title=meta['display_title'],
                    trashed_at=meta['trashed_at'], metadata_revision=meta['revision'],
                    metadata_updated_at=meta['updated_at'], snippet=snippet,
                    matched_field=hits[0]['field'] if hits else None, hits=hits, more_hits=more_hits)

    def resolve_locator(self, task, locator):
        if not isinstance(locator, dict) or len(encoded(locator)) > MAX_LOCATOR_BYTES:
            raise CoreError('历史定位对象无效。', 'invalid_locator')
        if locator.get('version') != LOCATOR_VERSION:
            raise CoreError('历史定位版本不支持。', 'unsupported_contract')
        if locator.get('root_id') != self.root_id or locator.get('session_id') != self.session_id:
            raise CoreError('历史定位属于旧会话或其他任务根，请重新搜索。', 'history_owner')
        unsigned = {k: v for k, v in locator.items() if k != 'signature'}
        signature = hmac.new(self.secret, encoded(unsigned, canonical=True), hashlib.sha256).hexdigest()
        if not isinstance(locator.get('signature'), str) or not hmac.compare_digest(signature, locator['signature']):
            raise CoreError('历史定位校验失败，请重新搜索。', 'invalid_locator')
        if locator.get('task_id') != task['id']:
            raise CoreError('历史定位与所选任务不匹配。', 'history_owner')
        current_path = self.store.directory(task['id']) / 'current.json'
        expected_task = self.task_baselines.get(task['id'])
        metadata_path = self.path(task['id'])
        metadata_hash = sha(safe_file(metadata_path, MAX_METADATA_BYTES)) if metadata_path.exists() else None
        replaced = ((expected_task is not None and sha(safe_file(current_path)) != expected_task) or
                    metadata_hash != self.metadata_baselines.get(task['id'], metadata_hash))
        meta = self.metadata(task['id'], task['kind'])
        current = (task['revision'], meta['revision'], self.store.history_epoch, content_revision(task))
        requested = tuple(locator.get(k) for k in ('task_revision', 'metadata_revision', 'epoch', 'content_revision'))
        if replaced or current != requested:
            return dict(status='stale', message='内容或记录已更新，已打开当前内容，请刷新搜索后重新定位。')
        return dict(status='located', locator=locator, task_id=task['id'], task_revision=task['revision'])

    def cursor(self, body):
        raw = encoded(body).hex()
        return raw+'.'+hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()

    def decode_cursor(self, value, binding, epoch):
        try:
            if not isinstance(value,str) or len(value)>2048: raise ValueError()
            raw, signature = value.split('.')
            if not hmac.compare_digest(hmac.new(self.secret,raw.encode(),hashlib.sha256).hexdigest(),signature): raise ValueError()
            body = json.loads(bytes.fromhex(raw))
            if (body['binding'] != binding or body['epoch'] != epoch or type(body['offset']) is not int or
                    not 0 <= body['offset'] <= MAX_SCAN_TASKS): raise ValueError()
            return body['offset']
        except (ValueError, KeyError, TypeError):
            raise CoreError('分页已失效，请重新查询。', 'invalid_cursor')

    def query(self, query, cancel=None):
        cancel = cancel or threading.Event()
        self.guard_root()
        owner = query.get('owner')
        if (not isinstance(owner,dict) or owner.get('root_id') != self.root_id or owner.get('session_id') != self.session_id or
                owner.get('scope') not in ('history','recent') or type(owner.get('view_revision')) is not int or owner['view_revision']<0):
            raise CoreError('查询归属已变化。', 'history_owner')
        task_id(owner.get('query_id'))
        if query.get('version') != QUERY_VERSION: raise CoreError('历史查询版本不支持。', 'unsupported_contract')
        raw = query.get('text','')
        if not isinstance(raw,str) or len(raw)>MAX_QUERY_CHARS or len(raw.encode())>MAX_QUERY_BYTES:
            raise CoreError('搜索最多512个字符且2048字节，输入已保留。', 'query_too_long')
        needle = search_view(raw)
        kind, status = query.get('kind'), query.get('status')
        if kind not in (None,*KINDS) or status not in (None,*STATUSES): raise CoreError('筛选条件不支持。', 'invalid_filter')
        if type(query.get('favorite',False)) is not bool or type(query.get('trash',False)) is not bool:
            raise CoreError('筛选条件无效。', 'invalid_filter')
        limit=query.get('limit',MAX_PAGE)
        if type(limit) is not int or not 1<=limit<=MAX_PAGE: raise CoreError('分页容量无效。', 'invalid_limit')
        binding=sha(encoded(dict(root=self.root_id,session=self.session_id,scope=owner['scope'],text=needle,
                    kind=kind,status=status,favorite=query.get('favorite',False),trash=query.get('trash',False),limit=limit)))
        epoch=self.store.history_epoch
        offset=self.decode_cursor(query['cursor'],binding,epoch) if query.get('cursor') else 0
        rows,errors=[],[]; scanned=used=0; limited=False; started=time.monotonic()
        # Scan only the explicitly owned root. No recursive discovery or persistent cache.
        with os.scandir(self.store.root) as entries:
            for entry in entries:
                if cancel.is_set(): raise Cancelled()
                try: identity=task_id(entry.name)
                except CoreError: continue
                if scanned>=MAX_SCAN_TASKS or time.monotonic()-started>MAX_SCAN_SECONDS:
                    limited=True;break
                scanned+=1
                try:
                    with self.store.mutex:
                        self.guard_root()
                        directory=self.store.directory(identity)
                        # Preflight both snapshots and text assets before bounded reads.
                        amount=0
                        for path in (directory/'current.json',directory/'previous.json',directory/'.history.json'):
                            if is_link(path): raise CoreError('快照路径不安全。','unsafe_path')
                            if path.exists(): amount+=path.stat().st_size
                        current_path = directory/'current.json'
                        if current_path.exists():
                            amount += current_path.stat().st_size  # external-replacement baseline read
                        if amount>MAX_SCAN_BYTES-used: limited=True;break
                        used+=amount
                        task,warning=self.store.read_directory(directory,identity)
                        if current_path.exists():
                            self.task_baselines[identity] = sha(safe_file(current_path))
                            while len(self.task_baselines) > MAX_SCAN_TASKS:
                                self.task_baselines.pop(next(iter(self.task_baselines)))
                        if task['kind']=='文字' and task.get('asset'):
                            asset=directory/self.store.asset_name(task)
                            size=asset.stat().st_size if asset.exists() else 0
                            if size>MAX_SCAN_BYTES-used: limited=True;break
                            used+=size
                            try:self.store.verify_asset(task,directory)
                            except (CoreError,OSError) as exc:warning=(warning+'\n' if warning else '')+str(exc)
                        elif task.get('asset'):
                            asset = directory/self.store.asset_name(task)
                            if is_link(asset) or not asset.is_file():
                                warning = (warning+'\n' if warning else '')+'封存来源缺失或路径不安全，已保存正文仍可回看。'
                        meta=self.metadata(identity,task['kind'])
                    if kind and task['kind']!=kind:continue
                    phase=business_status(task)
                    if status and phase!=status:continue
                    if query.get('favorite',False) and not meta['favorite']:continue
                    if (meta['trashed_at'] is not None)!=query.get('trash',False):continue
                    row=self.summary(task,meta,warning,needle,cancel,started+MAX_SCAN_SECONDS,epoch)
                    if row: rows.append(row)
                except (CoreError,OSError,ValueError) as exc:
                    if isinstance(exc, Cancelled):
                        raise
                    if getattr(exc, 'code', None) == 'history_deadline':
                        limited = True
                        break
                    if len(errors)<20:errors.append(dict(task_id=identity,message=str(exc)[:240],code=getattr(exc,'code','history_io')))
                if scanned%32==0:time.sleep(0)  # cancellation/fairness point, no GUI work
        if cancel.is_set():raise Cancelled()
        if epoch!=self.store.history_epoch:raise CoreError('任务正在更新，请刷新记录。','history_changed')
        rows.sort(key=lambda row:(-row['updated'],row['id']))
        page=rows[offset:offset+limit]
        result=dict(version=QUERY_VERSION,owner=owner,epoch=epoch,status='limited' if limited else 'ok',
                    items=page,errors=errors,matched_count=len(rows),scanned=scanned,scan_bytes=used,
                    elapsed_ms=(time.monotonic()-started)*1000,limited=limited,next_cursor=None)
        # Trim only complete rows, never whole bodies into the wire.
        while len(encoded(result))>MAX_PAGE_BYTES-1024 and result['items']:result['items'].pop()
        next_offset=offset+len(result['items'])
        if next_offset<len(rows):result['next_cursor']=self.cursor(dict(binding=binding,epoch=epoch,offset=next_offset))
        return result
