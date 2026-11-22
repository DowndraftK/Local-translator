"""Shared authoritative audio workflow, using the existing store and operation lease."""
import json
import math
import threading
import time
import uuid
import wave
from copy import deepcopy
from pathlib import Path

from . import Cancelled, CoreError
from .audio import (CONTRACT, SOURCE, MAX_SOURCE_BYTES, MAX_SAMPLES, MAX_CUE_BYTES, MAX_CUES,
                    REFINE_SAMPLES, SUFFIXES, new_cue, subtitle_text, validate_audio_task)
from .contracts import default_recipe, recipe_binding, sha, trim, validate_recipe
from .image_tasks import ImageTasks
from .languages import speech_language
from .resources import EXECUTION, compare_binding, no_links, atomic_json
from .store import atomic_copy, file_identity, safe_file
from .tasks import FOUNDATION_EPOCH


def identity():
    return str(uuid.uuid4()).upper()


class AudioTasks(ImageTasks):
    def __init__(self, *args, audio_adapter=None, resources=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.audio_adapter = audio_adapter; self.resources = resources
        self.audio_selection = None; self._audio_save_failure = None
        journal = self.store.root / 'audio-selection.json'
        if journal.exists():
            try:
                value = json.loads(safe_file(journal, 16384))
                if value.get('contract') != 'audio-selection-v1': raise ValueError('版本不兼容')
                source = value['source']; path = self.store.root / 'incoming' / (value['token'] + source['suffix'])
                no_links(path)
                if file_identity(path, MAX_SOURCE_BYTES) != (source['byteCount'], source['fingerprint']):
                    raise ValueError('来源不匹配')
                self.audio_selection = dict(value, path=str(path))
            except (CoreError, OSError, KeyError, ValueError) as exc:
                self.warning = '音频暂存未能恢复，原文件保留。' + str(exc)

    def _persist(self, changed):
        if not self.task or self.task['kind'] != '音频': return super()._persist(changed)
        audio = self.task['audio']; capture = audio.get('recording')
        if capture:
            water = capture['watermarks']
            water['stable'] = max((cue['endSample'] for cue in audio['cues']), default=0)
            water['translated'] = 0
            for cue in audio['cues']:
                if cue['state'] != 'completed': break
                water['translated'] = max(water['translated'], cue['endSample'])
            if audio['asrComplete']: water['asrConsumed'] = water['durable']
        path = self.store.directory(self.task['id']) / 'current.json'
        baseline = sha(safe_file(path)) if path.is_file() else None
        try:
            super()._persist(changed); self._audio_save_failure = None
        except CoreError:
            self._audio_save_failure = baseline
            raise

    def retry_audio_save(self):
        with self.lock:
            self.idle(); self._audio()
            path = self.store.directory(self.task['id']) / 'current.json'
            if not self.save_blocked or not self._audio_save_failure or sha(safe_file(path)) != self._audio_save_failure:
                raise CoreError('磁盘任务已改变或没有可重试保存，请保留导出内容。', 'save_blocked')
            self.store.verify_asset(self.task, self.store.directory(self.task['id']))
            self.save_blocked = False; self.warning = None
            return self.save()

    def select_audio(self, path, generation, cancel):
        with self.lock: self.idle(); self.active = (generation, cancel)
        sealed = None; accepted = False
        try:
            path = Path(path).absolute(); no_links(path)
            if path.suffix.lower() not in SUFFIXES or any(p.name.endswith('.incomplete') for p in (path, *path.parents)):
                raise CoreError('请选择WAV、AIFF、MP3、M4A、FLAC或OGG音频。', 'audio_format')
            count, fingerprint = file_identity(path, MAX_SOURCE_BYTES, cancel)
            if not count: raise CoreError('音频文件为空。', 'audio_empty')
            token = uuid.uuid4().hex
            sealed = self.store.root / 'incoming' / (token + path.suffix.lower())
            atomic_copy(path, sealed, count, fingerprint, cancel)
            source = dict(contract=SOURCE, id=identity(), file=path.name, originalPath=str(path), suffix=path.suffix.lower(),
                          fingerprint=fingerprint, byteCount=count, ownership='application_sealed_copy')
            selected = dict(contract='audio-selection-v1', token=token, source=source)
            with self.lock:
                self._current(generation)
                journal = self.store.root / 'audio-selection.json'
                if journal.exists():
                    old = safe_file(journal, 16384)
                    self.store.write(old, self.store.root / 'incoming' / ('audio-selection-' + uuid.uuid4().hex + '.json'))
                self.store.write(json.dumps(selected, ensure_ascii=False).encode(), journal)
                self.audio_selection = dict(selected, path=str(sealed)); accepted = True
                return selected
        finally:
            if sealed and not accepted: sealed.unlink(missing_ok=True)
            with self.lock: self.active = None

    def selected_audio(self):
        return {k: deepcopy(v) for k, v in self.audio_selection.items() if k != 'path'} if self.audio_selection else None

    def create_audio(self, token, setup=None):
        with self.lock:
            self.idle(); selected = self.audio_selection
            if not selected or token != selected['token']: raise CoreError('音频选择已改变。', 'selection_mismatch')
            source = deepcopy(selected['source']); raw = Path(selected['path']); no_links(raw)
            if file_identity(raw, MAX_SOURCE_BYTES) != (source['byteCount'], source['fingerprint']):
                raise CoreError('暂存音频已改变。', 'source_mismatch')
            now = time.time() - FOUNDATION_EPOCH; task_id = identity()
            recipe = setup['recipe'] if setup else self.new_recipe('en-zh')
            task = dict(id=task_id, formatVersion=4, revision=1, created=now, updated=now, name=source['file'], kind='音频',
                input='', configuration=recipe, drafts={}, asset=dict(originalPath=source['originalPath'],
                    filename='source-' + source['fingerprint'] + source['suffix'],
                    byteCount=source['byteCount'], fingerprint=source['fingerprint']),
                audio=dict(contract=CONTRACT, source=source, pcm=None, phase='saved', asrComplete=False,
                    cues=[], checkpoint=dict(sample=0, cueCount=0), bindings={}, executions=[], history=[]))
            if setup:
                task['execution'] = setup['execution']
                task['audio']['asrModel'] = setup['asr_model']
            else:
                self.apply_new_execution(task)
            self.store.save(task, raw)
            self.task = task; self.warning = None; self.save_blocked = False; self.durable_revision = 1
            return self.snapshot()

    def _audio(self):
        if not self.task or self.task['kind'] != '音频': raise CoreError('当前不是音频任务。', 'task_kind_mismatch')
        return self.task['audio']

    def open(self, task_id):
        result = super().open(task_id)
        if self.task['kind'] == '音频' and self.task['audio']['pcm']:
            try: self.pcm_path()
            except (CoreError, OSError) as exc: self.warning = str(exc); self.save_blocked = True
            result = self.snapshot()
        return result

    def pcm_path(self):
        pcm = self._audio()['pcm']
        if not pcm: raise CoreError('请先主动识别以准备规范音频。', 'audio_incomplete')
        path = self.store.directory(self.task['id']) / pcm['filename']; no_links(path)
        if file_identity(path, MAX_SAMPLES * 2 + 44) != (pcm['byteCount'], pcm['fingerprint']):
            raise CoreError('规范音频缺失或被替换，已保存字幕可导出。', 'source_mismatch')
        with wave.open(str(path), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) != (16000, 1, 2, pcm['samples']):
                raise CoreError('规范音频样本或格式不匹配。', 'audio_incomplete')
        return path

    def _binding(self, capability):
        if not self.resources: raise CoreError('请在偏好设置选择、保存并主动检查资源。', 'audio_binding')
        a = self._audio()
        execution = None
        if capability.startswith('speech.') and a.get('asrModel'):
            language = speech_language(a['asrModel'],self.task['configuration']['direction'])
            execution = dict(EXECUTION, language=language)
        binding = json.loads(self.resources.make_binding(capability,execution))
        saved = a['bindings'].get(capability)
        if saved and compare_binding(saved, binding) != 'identical':
            raise CoreError('资源位置或内容已改变，请明确另建任务并保留旧结果。', 'audio_binding')
        return binding

    def _writable(self):
        self.idle(); self._audio()
        if self.save_blocked: raise CoreError('任务保存受阻，请保留内容并导出。', 'save_blocked')
        if self.store.history.metadata(self.task['id'], '音频')['trashed_at'] is not None:
            raise CoreError('请先恢复此记录。', 'task_trashed')
        self.store.verify_asset(self.task, self.store.directory(self.task['id']))

    def recognize_audio(self, generation, cancel, changed, refine=False):
        with self.lock:
            self._writable(); a = self._audio()
            if a.get('recording') and not a['pcm']:
                raise CoreError('请先主动恢复封存录音，再继续识别。', 'audio_incomplete')
            if self.task['drafts']: raise CoreError('请先应用或明确放弃字幕草稿。', 'unapplied_draft')
            if a['asrComplete'] and not refine: raise CoreError('识别已结束，可主动翻译或录后精修。', 'audio_complete')
            if not self.audio_adapter: raise CoreError('当前平台音频适配器缺失。', 'audio_model')
            capability = 'speech.refine' if refine else 'speech.file_asr'
            previous = a['executions'][-1] if a['executions'] else None
            if not refine and previous and a['checkpoint']['sample'] and previous['adapter'] != self.audio_adapter.identity:
                raise CoreError('检查点的音频适配代码身份已改变，请保留结果并另建任务。', 'audio_binding')
            binding = self._binding(capability)
            decode_binding = self._binding('audio.decode') if not a['pcm'] else None
            if cancel.is_set(): raise Cancelled()
            if refine:
                self.pcm_path()
                if a['pcm']['samples'] > REFINE_SAMPLES: raise CoreError('本版精修最多10分钟。', 'audio_limit')
                if (self.task.get('parent') or {}).get('purpose') != 'refinement':
                    self._fork_refinement()
                    a = self._audio()
            if len(a['executions']) >= 64: raise CoreError('执行记录已达64次上限，请另建任务。', 'audio_limit')
            self.active = (generation, cancel); a['bindings'][capability] = binding
            if decode_binding: a['bindings']['audio.decode'] = decode_binding
            execution = dict(id=generation, stage='refine' if refine else 'asr', adapter=self.audio_adapter.identity,
                source=a['source']['fingerprint'], binding=sha(json.dumps(binding, sort_keys=True).encode()),
                at=time.time(), status='running', evidence=[])
            a['executions'].append(execution); a.pop('error', None)
        try:
            if not a['pcm']:
                with self.lock: a['phase'] = 'decoding'; self._persist(changed)
                decoded = []
                def decode_event(event, folder):
                    if event.get('event') == 'decoded': decoded.append((event['pcm'], folder / 'audio.wav'))
                self.audio_adapter.execute('decode', self.store.verify_asset(self.task, self.store.directory(self.task['id'])),
                    decode_binding, cancel, decode_event, suffix=a['source']['suffix'])
                if len(decoded) != 1: raise CoreError('解码未发布完整规范音频。', 'audio_decode')
                pcm, prepared = decoded[0]
                proposed = deepcopy(self.task); proposed['audio']['pcm'] = pcm; validate_audio_task(proposed)
                target = self.store.directory(self.task['id']) / pcm['filename']
                atomic_copy(prepared, target, pcm['byteCount'], pcm['fingerprint'], cancel)
                with self.lock: self._current(generation); a['pcm'] = pcm; self._persist(changed)
            path = self.pcm_path()
            if a['pcm'].get('exactZeroPCM') is True:
                with self.lock:
                    self._current(generation); a['asrComplete'] = True; a['phase'] = 'completed'
                    a['checkpoint'] = dict(sample=a['pcm']['samples'], cueCount=0)
                    execution.update(status='completed', skippedInference='exact-zero-PCM', pcm=a['pcm']['fingerprint'])
                    self._persist(changed); return 'completed'
            with self.lock:
                self._current(generation)
                cp = a['checkpoint']; prefix = cp['cueCount'] if not refine else 0
                if len(a['cues']) > prefix:
                    a['history'].append(dict(kind='replayed-tail', operation=generation, cues=deepcopy(a['cues'][prefix:])))
                    a['cues'] = a['cues'][:prefix]
                offset = cp['sample'] if not refine else 0
                execution.update(offset=offset, pcm=a['pcm']['fingerprint'])
                a['phase'] = 'refining' if refine else 'recognizing'; a['asrComplete'] = False
                self._persist(changed)
            complete = [False]
            def receive(event, folder):
                with self.lock:
                    self._current(generation)
                    if not execution['evidence']: execution['evidence'].append(str(folder.relative_to(self.store.root.parent)))
                    kind = event.get('event')
                    if kind == 'cue':
                        row = event['row']; cue_id = str(uuid.uuid5(uuid.UUID(generation), str(row['id']))).upper()
                        cue = new_cue(cue_id, row, a['pcm']['samples'])
                        proposed = deepcopy(self.task); proposed['audio']['cues'].append(cue); validate_audio_task(proposed)
                        a['cues'].append(cue); self._persist(changed)
                    elif kind == 'checkpoint':
                        checkpoint = event['checkpoint']
                        if checkpoint and offset <= checkpoint['sample'] <= a['pcm']['samples']:
                            count = prefix + checkpoint['segment_id']
                            if count <= len(a['cues']):
                                a['checkpoint'] = dict(sample=checkpoint['sample'], cueCount=count,
                                    pendingTokens=json.loads(checkpoint['pending_tokens']),
                                    mediaDigest=checkpoint['media_digest'], resourcesDigest=checkpoint['resources_digest'],
                                    pcm=a['pcm']['fingerprint'], execution=generation)
                                self._persist(changed)
                    elif kind == 'complete': complete[0] = event['complete']; execution['result'] = event.get('evidence')
                    elif kind == 'state':
                        execution['runtime'] = event.get('evidence'); execution['pendingTail'] = event.get('pending', False)
                    elif kind == 'released': execution['release'] = event['evidence']
            self.audio_adapter.execute('refine' if refine else 'asr', path, binding, cancel, receive,
                samples=a['pcm']['samples'], audio_hash=a['pcm']['fingerprint'], offset=offset,
                pending_tokens=cp.get('pendingTokens', []) if not refine else [])
            with self.lock:
                self._current(generation)
                if not complete[0]: raise CoreError('后端未确认全部音频结束。', 'audio_incomplete')
                a['asrComplete'] = True; a['phase'] = 'completed'; execution['status'] = 'completed'
                a.pop('temporary', None)
                repeated = sum(left['source'].casefold() == right['source'].casefold() for left, right in zip(a['cues'], a['cues'][1:]))
                a['qualityIssues'] = ([f'存在{repeated}组相邻相同字幕，请回放核对真实重复或模型额外输出。'] if repeated else [])
                a['checkpoint'] = dict(sample=a['pcm']['samples'], cueCount=len(a['cues']))
                self._persist(changed); return 'completed'
        except (Cancelled, CoreError, OSError, ValueError, TypeError) as exc:
            with self.lock:
                a['phase'] = 'stopped' if isinstance(exc, Cancelled) else 'failed'
                execution['status'] = a['phase']; a['error'] = '已停止，主动继续将从已保存静音检查点重放尾部。' if isinstance(exc, Cancelled) else str(exc)
                if not self.save_blocked: self._persist(changed)
            return 'cancelled' if isinstance(exc, Cancelled) else 'failed'
        finally:
            with self.lock: self.active = None

    def _fork_refinement(self):
        old = self.task; a = old['audio']; pcm_path = self.pcm_path()
        task = deepcopy(old); now = time.time() - FOUNDATION_EPOCH
        task.update(id=identity(), revision=1, created=now, updated=now, name=old['name'] + ' · 精修', drafts={},
            parent=dict(id=old['id'], revision=old['revision'], purpose='refinement', asrComplete=a['asrComplete'],
                        phase=a['phase'], pcm=a['pcm']['fingerprint'], range=[0, a['pcm']['samples']]))
        task['audio'].update(cues=[], checkpoint=dict(sample=0, cueCount=0), asrComplete=False,
                             phase='saved', history=[], executions=[])
        if task['audio'].get('recording'):
            task['audio']['recording']['watermarks'].update(asrConsumed=0, stable=0, translated=0)
        target = self.store.directory(task['id']); target.mkdir()
        atomic_copy(pcm_path, target / a['pcm']['filename'], a['pcm']['byteCount'], a['pcm']['fingerprint'])
        capture = a.get('recording')
        if capture:
            (target / 'capture').mkdir()
            for chunk in capture['chunks']:
                atomic_copy(self.store.directory(old['id']) / chunk['file'], target / chunk['file'], chunk['bytes'], chunk['sha256'])
        self.store.save(task, self.store.verify_asset(old, self.store.directory(old['id'])))
        self.task = task; self.durable_revision = 1

    def audio_cue(self, cue_id, revision):
        self._writable()
        cue = next((c for c in self._audio()['cues'] if c['id'] == cue_id), None)
        if not cue or cue['revision'] != revision: raise CoreError('字幕有效修订已改变。', 'cue_revision_mismatch')
        return cue

    def audio_draft(self, cue_id, revision, body):
        with self.lock:
            cue = self.audio_cue(cue_id, revision)
            if not isinstance(body, str) or len(body.encode()) > MAX_CUE_BYTES: raise CoreError('单行字幕草稿最多2048字节。', 'audio_limit')
            draft = dict(id=identity(), baseRevision=cue['revision'], text=body)
            proposed = deepcopy(self.task); proposed['drafts'][cue_id] = draft; validate_audio_task(proposed)
            self.task['drafts'][cue_id] = draft
            return self.save()

    def audio_apply(self, cue_id, revision, discard=False):
        with self.lock:
            cue = self.audio_cue(cue_id, revision); draft = self.task['drafts'].get(cue_id)
            if not draft or draft['baseRevision'] != revision: raise CoreError('没有属于当前修订的已保存草稿。', 'draft_revision_mismatch')
            if not discard:
                proposed = deepcopy(self.task)
                next(c for c in proposed['audio']['cues'] if c['id'] == cue_id)['source'] = draft['text']
                # Validate source capacity independently of the old translation, which this edit invalidates.
                candidate = next(c for c in proposed['audio']['cues'] if c['id'] == cue_id)
                candidate['state'] = 'pending'; validate_audio_task(proposed)
                self._audio()['history'].append(dict(kind='correction', cue=deepcopy(cue), draft=deepcopy(draft)))
                cue.update(source=draft['text'], revision=identity(), reviewed=False, reviewRevision=None,
                           state='pending', translationRevision=None)
                cue.pop('result', None); cue.pop('error', None)
            del self.task['drafts'][cue_id]
            return self.save()

    def audio_review(self, cue_id, revision, value):
        with self.lock:
            cue = self.audio_cue(cue_id, revision)
            if type(value) is not bool or cue_id in self.task['drafts']: raise CoreError('请先应用或放弃校正草稿。', 'unapplied_draft')
            cue.update(reviewed=value, reviewRevision=revision if value else None)
            return self.save()

    def run(self, generation, cancel, changed, started=lambda: None):
        if not self.task or self.task['kind'] != '音频': return super().run(generation, cancel, changed, started)
        with self.lock:
            self._writable(); a = self._audio()
            if self.task['drafts']: raise CoreError('请先应用或放弃字幕草稿。', 'unapplied_draft')
            if not a['asrComplete']: raise CoreError('请先主动继续识别，再翻译有效字幕。', 'audio_incomplete')
            recipe = deepcopy(self.task['configuration']); validate_recipe(recipe)
            self.active = (generation, cancel); self.engine = self.engine_factory(cancel=cancel)
        try:
            for cue in a['cues']:
                if cue['state'] == 'completed' or not trim(cue['source']): continue
                with self.lock:
                    self._current(generation); cue['state'] = 'processing'; cue.pop('error', None); self._persist(changed)
                try:
                    result = self.engine.translate(trim(cue['source']), recipe)
                    with self.lock:
                        self._current(generation)
                        proposed = deepcopy(self.task); candidate = next(c for c in proposed['audio']['cues'] if c['id'] == cue['id'])
                        candidate.update(result=result, state='completed', translationRevision=cue['revision'])
                        validate_audio_task(proposed)
                        cue.update(result=result, state='completed', translationRevision=cue['revision'])
                except CoreError as exc:
                    with self.lock:
                        self._current(generation); cue.update(state='failed', error=str(exc)); self._persist(changed)
                    if exc.stops_scheduling: return 'failed'
                with self.lock: self._persist(changed)
            return 'completed' if all(c['state'] == 'completed' or not trim(c['source']) for c in a['cues']) else 'partial'
        except Cancelled:
            with self.lock:
                for cue in a['cues']:
                    if cue['state'] == 'processing': cue['state'] = 'stopped'
                if not self.save_blocked: self._persist(changed)
            return 'cancelled'
        finally:
            with self.lock: self.active = None; self.engine = None

    def play_audio(self, generation, cancel, receive, start_sample=0):
        with self.lock:
            self.idle(); a = self._audio(); self._binding('audio.playback'); path = self.pcm_path()
            if type(start_sample) is not int or not 0 <= start_sample < a['pcm']['samples']:
                raise CoreError('播放起点超出音频。', 'audio_time')
            self.active = (generation, cancel)
        try:
            self.audio_adapter.play(path, cancel, receive, generation, start_sample)
            return 'completed'
        finally:
            with self.lock: self.active = None

    def audio_control(self, generation, action, seconds=None):
        if not self.active or self.active[0] != generation or action not in ('pause', 'resume', 'seek', 'stop'):
            raise CoreError('播放控制归属已改变。', 'audio_playback')
        if action == 'seek' and (type(seconds) not in (float, int) or not math.isfinite(seconds) or
                not 0 <= seconds < self._audio()['pcm']['samples'] / 16000):
            raise CoreError('播放定位超出音频范围。', 'audio_time')
        self.audio_adapter.control(action, generation, seconds)

    def export_audio(self, format):
        with self.lock: self._audio(); return subtitle_text(deepcopy(self.task), format)

    def export(self):
        return self.export_audio('txt') if self.task and self.task['kind'] == '音频' else super().export()
