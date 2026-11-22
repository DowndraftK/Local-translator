"""Live recording under the existing TaskStore and single worker operation."""
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid
import wave

from . import Cancelled, CoreError
from .audio import MAX_SAMPLES, PCM, SOURCE as FILE_SOURCE, new_cue, validate_audio_task
from .audio_tasks import AudioTasks, identity
from .capture import CHUNK_SAMPLES, CONTRACT, FEED, MAX_CHUNKS, SOURCE, recording, validate_source
from .contracts import default_recipe, recipe_binding, sha, trim
from .resources import atomic_json, no_links
from .store import atomic_copy, atomic_write, file_identity, safe_file
from .tasks import FOUNDATION_EPOCH
from .ollama import Ollama


class CaptureTasks(AudioTasks):
    def __init__(self, *args, capture_adapter=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.capture_adapter = capture_adapter
        self.capture_description = None
        self.capture_generation = 0
        self.capture_runtime = None

    def describe_capture(self):
        with self.lock:
            self.idle()
            if not self.capture_adapter:
                raise CoreError('当前Mac采集helper不可用。', 'capture_unavailable')
            self.capture_generation += 1
            facts = self.capture_adapter.describe()
            self.capture_description = dict(facts, generation=self.capture_generation, id=identity())
            return deepcopy(self.capture_description)

    def open(self, task_id):
        result = super().open(task_id)
        value = recording(self.task)
        if value:
            try:
                for chunk in value['chunks']:
                    path = self.store.directory(self.task['id']) / chunk['file']
                    no_links(path)
                    if file_identity(path, CHUNK_SAMPLES * 2) != (chunk['bytes'], chunk['sha256']):
                        raise CoreError('录音分块被替换，保留可读字幕。', 'capture_chunk')
            except (CoreError, OSError) as exc:
                self.warning = str(exc)
                self.save_blocked = True
            result = self.snapshot()
        return result

    def capture_control(self, generation, lease, task, session, epoch, action):
        with self.lock:
            runtime = self.capture_runtime
            if (not runtime or runtime['generation'] != generation or runtime['lease'] != lease or
                    runtime['task'] != task or runtime['session'] != session or runtime['epoch'] != epoch):
                raise CoreError('采集控制不属于当前会话或epoch。', 'capture_owner')
            state = recording(self.task)['state']
            allowed = {'pause': ('recording', 'starting'), 'resume': ('paused',),
                       'stop': ('preparing', 'waiting_permission', 'starting', 'recording', 'pausing', 'paused')}
            if action not in allowed or state not in allowed[action]:
                raise CoreError('当前采集状态不接受此动作。', 'capture_state')
            if action=='stop' and state=='preparing':
                self.active[1].set()
                if self.engine:
                    self.engine.cancel()
                return
            try:
                runtime['controls'].put_nowait((action, str(uuid.uuid4())))
            except queue.Full as exc:
                raise CoreError('已有采集控制等待确认。', 'capture_backpressure') from exc

    def _new_recording(self, kind, device_uid, generation, owner, setup=None):
        description = self.capture_description
        if not description or description['generation'] != generation:
            raise CoreError('请主动刷新设备，再选择本次声音来源。', 'capture_selection_changed')
        source = dict(contract=SOURCE, id=identity(), kind=kind, generation=generation,
                      permission=dict(microphone=description['microphonePermission'], system_audio=description['systemAudioPermission']))
        if kind == 'microphone':
            device = next((d for d in description['devices'] if d['uid'] == device_uid and d['alive']), None)
            if not device:
                raise CoreError('选定麦克风不在当前设备列表中。', 'capture_source')
            source.update(deviceUID=device_uid, name=device['name'], selectedDevice=deepcopy(device))
        elif kind == 'system_audio':
            source.update(name='电脑声音 · 系统允许捕获的整机播放音频', scope='all_capturable_system_audio')
        validate_source(source)
        now = time.time() - FOUNDATION_EPOCH
        task_id = identity()
        value = dict(contract=CONTRACT, session=str(uuid.uuid4()), source=deepcopy(source),
                     state='preparing', owner=dict(owner, task=task_id), chunks=[], epochs=[], events=[],
                     watermarks=dict(received=0, durable=0, asrConsumed=0, stable=0, translated=0, finalized=0),
                     createdWall=time.time(), activeEpoch=0, pausedWallSeconds=0.0,
                     helperIdentity=self.capture_adapter.identity, adapterIdentity=self.audio_adapter.identity)
        task = dict(id=task_id, formatVersion=5, revision=1, created=now, updated=now,
                    name=('麦克风' if kind == 'microphone' else '电脑声音') + ' · ' + time.strftime('%Y-%m-%d %H-%M-%S'),
                    kind='音频', input='', configuration=setup['recipe'] if setup else self.new_recipe('en-zh'), drafts={}, asset=None,
                    audio=dict(contract='audio-task-v2', source=source, pcm=None, recording=value,
                               phase='preparing', asrComplete=False, cues=[], checkpoint=dict(sample=0, cueCount=0),
                               bindings={}, executions=[], history=[]))
        if self.task:
            task['parent'] = dict(id=self.task['id'], revision=self.task['revision'], purpose='new-recording')
        if setup:
            task['execution'] = setup['execution']
            task['audio']['asrModel'] = setup['asr_model']
        else:
            self.apply_new_execution(task)
        self.store.save(task)
        self.task = task
        self.warning = None
        self.save_blocked = False
        self.durable_revision = 1
        return value

    def _feed(self):
        value = recording(self.task)
        directory = self.store.directory(self.task['id'])
        atomic_json(directory / 'capture-feed.json', dict(contract=FEED, session=value['session'], task=self.task['id'],
            samples=value['watermarks']['durable'], chunks=value['chunks'], events=value['events'],
            warmup_requested=bool(value.get('preparation',{}).get('warmup_requested')),
            closed=value['state'] in ('finalizing', 'sealed', 'interrupted', 'failed', 'save_blocked')))

    def _commit_chunk(self, data, epoch, changed):
        value = recording(self.task)
        water = value['watermarks']
        if len(value['chunks']) >= MAX_CHUNKS or water['durable'] + len(data) // 2 > MAX_SAMPLES:
            raise CoreError('录音达到分块或30分钟容量上限。', 'capture_limit')
        index = len(value['chunks'])
        chunk = dict(sequence=index, file=f'capture/{index:06d}.pcm', offset=water['durable'],
                     samples=len(data) // 2, bytes=len(data), sha256=sha(data), epoch=epoch, committedWall=time.time())
        directory = self.store.directory(self.task['id']) / 'capture'
        no_links(directory)
        directory.mkdir(exist_ok=True)
        target = directory / f'{index:06d}.pcm'
        if target.exists():
            raise CoreError('录音分块路径已存在，未覆盖原字节。', 'capture_chunk')
        atomic_write(data, target)
        value['chunks'].append(chunk)
        previous = water['durable']
        water['durable'] += chunk['samples']
        try:
            self._persist(lambda snapshot: changed(snapshot) if not snapshot['save_blocked'] else None)
        except CoreError:
            value['chunks'].pop()
            water['durable'] = previous
            value['state'] = 'save_blocked'
            changed(self.snapshot())
            raise
        self._feed()

    def _seal_recording(self, changed):
        value = recording(self.task)
        samples = value['watermarks']['durable']
        if self.task['audio']['pcm']:
            self.pcm_path()
            return
        if not samples:
            value['state'] = 'failed'
            self.task['audio']['phase'] = 'failed'
            self.task['audio'].setdefault('error', '录音未收到可封存的实际样本。')
            self._persist(changed)
            return
        directory = self.store.directory(self.task['id'])
        temporary = directory / ('capture-seal-' + uuid.uuid4().hex + '.wav')
        nonzero = False
        with temporary.open('xb') as raw:
            with wave.open(raw, 'wb') as output:
                output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                for chunk in value['chunks']:
                    path = directory / chunk['file']
                    no_links(path)
                    data = safe_file(path, CHUNK_SAMPLES * 2)
                    if (len(data), sha(data)) != (chunk['bytes'], chunk['sha256']):
                        raise CoreError('录音分块被替换，请保留已保存内容。', 'capture_chunk')
                    nonzero = nonzero or any(data)
                    output.writeframesraw(data)
            raw.flush()
            os.fsync(raw.fileno())
        count, digest = file_identity(temporary, MAX_SAMPLES * 2 + 44)
        if count != 44 + samples * 2:
            raise CoreError('录音封存样本与清单不符。', 'capture_watermark')
        pcm = dict(contract=PCM, rate=16000, channels=1, sampleWidth=2, samples=samples,
                   fingerprint=digest, byteCount=count, filename='pcm-' + digest + '.wav', exactZeroPCM=not nonzero,
                   media=dict(captureSession=value['session'], sourceKind=value['source']['kind'],
                              clock='concatenated actual 16kHz samples, paused wall intervals excluded',
                              epochs=deepcopy(value['epochs'])))
        target = directory / pcm['filename']
        if target.exists():
            no_links(target)
            if file_identity(target, MAX_SAMPLES * 2 + 44) != (count, digest):
                raise CoreError('同名录音资产被替换，未覆盖原文件。', 'source_mismatch')
        else:
            atomic_copy(temporary, target, count, digest)
        source = dict(contract=FILE_SOURCE, id=value['source']['id'], file=self.task['name'] + '.wav', suffix='.wav',
                      fingerprint=digest, byteCount=count, ownership='application_recorded',
                      originalPath='capture://' + value['source']['kind'] + '/' + value['session'])
        # One immutable WAV is both the original captured asset and canonical PCM.
        self.task['asset'] = dict(filename=pcm['filename'], originalPath=source['originalPath'],
                                  byteCount=count, fingerprint=digest)
        self.task['audio'].update(source=source, pcm=pcm)
        value['watermarks']['finalized'] = samples
        value['state'] = 'sealed'
        value['sealedWall'] = time.time()
        self.task['audio']['phase'] = 'completed' if self.task['audio']['asrComplete'] else 'failed' if self.task['audio'].get('error') else 'stopped'
        self._persist(changed)
        temporary.unlink()

    def recover_recording(self, generation, cancel, changed):
        with self.lock:
            self._writable()
            if not recording(self.task):
                raise CoreError('当前任务没有采集清单。', 'capture_source')
            self.active = (generation, cancel)
        try:
            with self.lock:
                if cancel.is_set():
                    raise Cancelled()
                self._seal_recording(changed)
            return 'completed'
        finally:
            with self.lock:
                self.active = None

    def run_capture(self, generation, lease, owner, source_kind, device_uid, selection_generation,
                    cancel, changed, setup=None):
        with self.lock:
            self.idle()
            if self.save_blocked or not self.capture_adapter or not self.audio_adapter:
                raise CoreError('请先完成保存并确认Mac采集适配可用。', 'capture_unavailable')
            if self.task and self.task['drafts']:
                raise CoreError('请先处理已保存字幕草稿，再开始新录音。', 'unapplied_draft')
            previous = recording(self.task)
            if previous and not self.task['audio']['pcm'] and previous['watermarks']['durable']:
                raise CoreError('请先恢复封存已保存录音，再更换来源。', 'audio_incomplete')
            # Check resources before any permission request or physical capture.
            binding = setup['binding'] if setup else json.loads(self.resources.make_binding('speech.continuous_asr')) if self.resources else None
            if not binding:
                raise CoreError('请在偏好设置保存并主动检查语音资源。', 'audio_binding')
            value = self._new_recording(source_kind, device_uid, selection_generation, owner, setup)
            audio = self.task['audio']
            audio['bindings']['speech.continuous_asr'] = binding
            self.active = (generation, cancel)
            runtime = dict(generation=generation, lease=lease, task=self.task['id'], session=value['session'],
                           epoch=0, controls=queue.Queue(maxsize=1))
            self.capture_runtime = runtime
            self._persist(changed)
            self._feed()
        process = None
        pending = bytearray()
        expected_sequence = 0
        asr_messages = queue.Queue(maxsize=128)
        asr_thread = None
        asr_done = False
        asr_ready = threading.Event()
        inference_ready = threading.Event()
        asr_preparation = {}
        asr_complete = False
        asr_cancel = threading.Event()
        translation_result = queue.Queue(maxsize=1)
        translator = None
        translation_halted = False
        capture_done = False
        stopping_at = None
        ready_at = None
        attempt_at = time.monotonic()
        permission_waiting = False
        control_wait = None
        pause_at = None
        failure = None
        translation_cancel = threading.Event()
        start = time.monotonic()
        model_ready_at = start
        directory = self.store.directory(self.task['id'])
        gpu_lock = directory / 'capture-gpu.lock'
        translation_pending = directory / 'capture-translation.pending'
        atomic_write(b'', gpu_lock)
        execution = dict(id=generation, stage='live', adapter=self.audio_adapter.identity,
                         source=value['source']['id'], at=time.time(), status='running', evidence=[])
        audio['executions'].append(execution)

        def asr_receive(event, folder):
            if event.get('event')=='ready':
                asr_ready.set()
            elif event.get('event')=='model_ready':
                asr_preparation.update(event)
            elif event.get('event')=='inference_ready' and event.get('capture_session')==value['session']:
                asr_preparation['streaming'] = event['evidence']
                inference_ready.set()
            asr_messages.put((event, str(folder)), timeout=2)

        def recognize():
            try:
                self.audio_adapter.execute('live', directory / 'capture-feed.json', binding, asr_cancel,
                    asr_receive, capture_session=value['session'], task_id=self.task['id'], gpu_lock=str(gpu_lock),
                    translation_pending=str(translation_pending))
            except Exception as exc:
                asr_messages.put((dict(event='asr_failed', message=str(exc)), None), timeout=2)
            finally:
                asr_messages.put((dict(event='asr_ended'), None), timeout=2)

        def translate(cue):
            result = None
            error = None
            try:
                if cue is None:
                    result = self.engine.prepare(self.task['configuration'],
                        inference_turn=lambda:self.audio_adapter.translation_turn(gpu_lock, translation_cancel))
                elif isinstance(self.engine, Ollama):
                    result = self.engine.translate(trim(cue['source']), self.task['configuration'],
                        inference_turn=lambda:self.audio_adapter.translation_turn(gpu_lock, translation_cancel))
                else:
                    with self.audio_adapter.translation_turn(gpu_lock, translation_cancel):
                        result = self.engine.translate(trim(cue['source']), self.task['configuration'])
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            finally:
                translation_pending.unlink(missing_ok=True)
            translation_result.put((cue['id'] if cue else None, cue['revision'] if cue else None, result, error))

        def flush():
            while pending:
                part = bytes(pending[:CHUNK_SAMPLES * 2])
                self._commit_chunk(part, runtime['epoch'], changed)
                del pending[:len(part)]

        def capture_event(event):
            nonlocal expected_sequence, ready_at, control_wait, capture_done, pause_at, permission_waiting
            if event.get('session') != value['session']:
                raise CoreError('采集回调不属于当前会话。', 'capture_owner')
            kind = event['event']
            if kind == 'pcm':
                if (event.get('epoch') != runtime['epoch'] or event.get('sequence') != expected_sequence or
                        event.get('offset') != value['watermarks']['received']):
                    raise CoreError('采集帧重复、丢序或epoch失配。', 'capture_epoch')
                data = base64.b64decode(event['data'], validate=True)
                if len(data) != event['samples'] * 2 or not 0 < len(data) <= 16000 or len(data) % 2:
                    raise CoreError('采集PCM格式或边界无效。', 'capture_chunk')
                if value['watermarks']['received'] + event['samples'] > MAX_SAMPLES:
                    raise CoreError('实际录音达到30分钟上限。', 'capture_limit')
                expected_sequence += 1
                value['watermarks']['received'] += event['samples']
                epoch = value['epochs'][-1]
                epoch.update(inputFrames=event['inputFrames'], inputRate=event['inputRate'],
                             inputChannels=event['inputChannels'])
                if event.get('hostTime') is not None:
                    epoch.update(lastHostTime=event['hostTime'], lastHostSeconds=event.get('hostSeconds'))
                if event.get('sourceClock'):
                    epoch['lastSourceClock'] = event['sourceClock']
                if 'firstFrameWall' not in epoch:
                    epoch.update(firstFrameWall=event['wall'], firstHostTime=event['firstHostTime'])
                    epoch['firstSourceClock'] = event.get('firstSourceClock')
                    value['state'] = 'recording'
                    audio['phase'] = 'recording'
                pending.extend(data)
                while len(pending) >= CHUNK_SAMPLES * 2:
                    self._commit_chunk(bytes(pending[:CHUNK_SAMPLES * 2]), runtime['epoch'], changed)
                    del pending[:CHUNK_SAMPLES * 2]
            elif kind == 'ready':
                if event['epoch'] != runtime['epoch'] + 1:
                    raise CoreError('采集启动epoch不连续。', 'capture_epoch')
                runtime['epoch'] = event['epoch']
                value['activeEpoch'] = runtime['epoch']
                value['epochs'].append(dict(epoch=runtime['epoch'], startSample=value['watermarks']['durable'],
                    source=event['source'], output=event['output'], readyWall=event['wall']))
                if pause_at is not None:
                    value['pausedWallSeconds'] += max(0, event['wall'] - pause_at)
                    pause_at = None
                value['state'] = 'starting'
                audio['phase'] = 'starting'
                audio.pop('notice', None)
                ready_at = time.monotonic()
                permission_waiting = False
                control_wait = None
                self._persist(changed)
            elif kind in ('paused', 'stopped', 'interrupted'):
                flush()
                if event['samples'] != value['watermarks']['durable'] or event['epoch'] != runtime['epoch']:
                    raise CoreError('暂停或停止边界与已保存样本不符。', 'capture_watermark')
                value['events'].append(dict(id=str(uuid.uuid4()), kind=kind, sample=event['samples'],
                    epoch=event['epoch'], wall_time=event['wall'], control=event.get('control')))
                if value['epochs']:
                    value['epochs'][-1].update(endSample=event['samples'], boundary=kind, boundaryWall=event['wall'])
                capture_done = kind != 'paused'
                value['state'] = 'finalizing' if capture_done else 'paused'
                audio['phase'] = value['state']
                if kind != 'paused' and pause_at is not None:
                    value['pausedWallSeconds'] += max(0, event['wall'] - pause_at)
                pause_at = event['wall'] if kind == 'paused' else None
                control_wait = None
                self._persist(changed)
                self._feed()
            elif kind in ('permission', 'waiting_permission'):
                value['permissionFacts'] = event
                value['state'] = 'waiting_permission'
                audio['phase'] = 'waiting_permission'
                if kind == 'waiting_permission':
                    permission_waiting = True
                    control_wait = None
                self._persist(changed)
            elif kind == 'route_changed':
                changes = value.setdefault('routeChanges', [])
                if len(changes) >= 64:
                    raise CoreError('设备路由变化达到64次上限，请保存录音后重新选择。', 'capture_limit')
                changes.append(event)
                audio['notice'] = '默认输入或输出设备已变化。本次保留原选定来源，请核对来源与执行详情。'
                self._persist(changed)
            elif kind == 'failure':
                value['permissionFacts'] = event
                raise CoreError(event.get('reason', '采集helper未完成。'), 'capture_device')

        try:
            self.engine = self.engine_factory(cancel=translation_cancel)
            self.engine.request_timeout = 180
            if cancel.is_set():
                raise Cancelled()
            if getattr(self.audio_adapter, 'supports_prepared_live', False):
                audio['notice'] = '正在准备语音识别和翻译，请等待录音开始。'
                with self.lock:
                    self._persist(changed)
                asr_thread = threading.Thread(target=recognize, name='capture-asr', daemon=False)
                asr_thread.start()
                preparation_started = time.monotonic()
                while not asr_ready.wait(.04):
                    if cancel.is_set():
                        raise Cancelled()
                    if not asr_thread.is_alive():
                        raise CoreError('语音识别器准备失败，请查看执行详情。', 'audio_process')
                    if time.monotonic()-preparation_started>180:
                        raise CoreError('语音识别器准备超过180秒。', 'audio_timeout')
                preparation = dict(ASRReadyWall=time.time(), startedWall=time.time()-(time.monotonic()-preparation_started))
                if cancel.is_set():
                    raise Cancelled()
                if hasattr(self.engine, 'prepare'):
                    with self.lock:
                        audio['notice'] = '语音识别已就绪，正在准备翻译模型。'
                        self._persist(changed)
                    preparation['translation'] = self.engine.prepare(self.task['configuration'],
                        inference_turn=lambda:self.audio_adapter.translation_turn(gpu_lock, translation_cancel))
                # The pinned acoustic calibration clip contains English.
                # Other languages load the model now and start alignment with their input.
                if binding['execution'].get('backend') is None and binding['execution']['language']=='en' and asr_preparation and (not asr_preparation.get('reused') or
                        preparation.get('translation',{}).get('elapsedSeconds',0)>=1):
                    preparation['warmup_requested'] = True
                    with self.lock:
                        value['preparation'] = preparation
                        audio['notice'] = '正在完成识别计算预热，请等待正在录音提示。'
                        self._persist(changed)
                        self._feed()
                    warmup_started = time.monotonic()
                    while not inference_ready.wait(.04):
                        if cancel.is_set():
                            raise Cancelled()
                        if not asr_thread.is_alive():
                            raise CoreError('识别计算预热失败。', 'audio_process')
                        if time.monotonic()-warmup_started>180:
                            raise CoreError('识别计算预热超过180秒。', 'audio_timeout')
                    preparation['streaming'] = asr_preparation['streaming']
                preparation['ASRModelReused'] = asr_preparation.get('reused')
                if cancel.is_set():
                    raise Cancelled()
                preparation['readyWall'] = time.time()
                model_ready_at = time.monotonic()
                with self.lock:
                    value['preparation'] = preparation
                    audio['notice'] = '识别与翻译已准备，正在接通所选声音来源。'
                    self._persist(changed)
                attempt_at = time.monotonic()
            native_source = dict(value['source'], owner=deepcopy(value['owner']),
                                 recipeBinding=recipe_binding(self.task['configuration']))
            process = self.capture_adapter.start(native_source, value['session'])
            while True:
                now = time.monotonic()
                with self.lock:
                    if now - start > 2400:
                        raise CoreError('本次录音墙钟达到40分钟上限。', 'capture_limit')
                    if pause_at is not None and time.time() - pause_at > 600:
                        raise CoreError('暂停等待达到10分钟上限，录音将封存。', 'capture_limit')
                    if ready_at and value['state'] == 'starting' and now - ready_at > 15:
                        raise CoreError('来源已就绪但15秒未收到实际音频帧。', 'capture_no_frames')
                    if ready_at is None and now - attempt_at > (120 if permission_waiting else 30):
                        raise CoreError('等待系统授权或采集启动达到本次时限。', 'capture_permission_timeout')
                    if control_wait and now - control_wait > 10:
                        raise CoreError('采集控制10秒未确认。', 'capture_control_timeout')
                    if stopping_at and now - stopping_at > 600:
                        raise CoreError('录音停止收尾超过10分钟。', 'capture_finalize_timeout')
                    if cancel.is_set() and stopping_at is None:
                        process.control('stop', 'operation_cancelled')
                        stopping_at = now
                        control_wait = now
                        value['state'] = 'stopping'
                        audio['phase'] = 'stopping'
                        asr_cancel.set()
                    try:
                        action, control = runtime['controls'].get_nowait()
                    except queue.Empty:
                        action = None
                    if action:
                        if control_wait:
                            raise CoreError('上一个采集控制尚未确认。', 'capture_state')
                        if action == 'resume':
                            if not binding.get('speech'):
                                self._binding('speech.continuous_asr')
                            if self.capture_adapter.identity != value['helperIdentity']:
                                raise CoreError('采集helper身份已改变，请保留旧录音并另建任务。', 'capture_owner')
                            ready_at = None
                            attempt_at = now
                            permission_waiting = False
                        process.control(action, control)
                        control_wait = now
                        value['state'] = {'pause': 'pausing', 'resume': 'starting', 'stop': 'stopping'}[action]
                        audio['phase'] = value['state']
                        if action == 'stop':
                            stopping_at = now
                        self._persist(changed)
                event = process.next_event(.02)
                if event:
                    with self.lock:
                        capture_event(event)
                if process.ended() and not capture_done:
                    raise CoreError('采集helper已退出，保留最后确认的录音前缀。', 'capture_process')
                with self.lock:
                    if not asr_thread and value['watermarks']['durable'] and not cancel.is_set():
                        asr_thread = threading.Thread(target=recognize, name='capture-asr', daemon=False)
                        asr_thread.start()
                    while True:
                        try:
                            message, folder = asr_messages.get_nowait()
                        except queue.Empty:
                            break
                        kind = message['event']
                        if folder and folder not in execution['evidence']:
                            execution['evidence'].append(folder)
                        if kind == 'cue' and not cancel.is_set():
                            cue = new_cue(str(uuid.uuid5(uuid.UUID(generation), str(message['row']['id']))).upper(),
                                          message['row'], value['watermarks']['durable'])
                            audio['cues'].append(cue)
                            value['watermarks']['stable'] = max(cue['endSample'], value['watermarks']['stable'])
                            self._persist(changed)
                        elif kind == 'checkpoint' and message.get('checkpoint') and not cancel.is_set():
                            cp = message['checkpoint']
                            if cp['sample'] <= value['watermarks']['durable'] and cp['segment_id'] <= len(audio['cues']):
                                audio['checkpoint'] = dict(sample=cp['sample'], cueCount=cp['segment_id'],
                                    pendingTokens=json.loads(cp['pending_tokens']), mediaDigest=cp['media_digest'],
                                    resourcesDigest=cp['resources_digest'], execution=generation)
                                self._persist(changed)
                        elif kind in ('state', 'live_progress'):
                            execution['runtime'] = message.get('evidence', {})
                            consumed = execution['runtime'].get('submitted_audio_samples', 0) or 0
                            value['watermarks']['asrConsumed'] = min(consumed, value['watermarks']['durable'])
                            processed = execution['runtime'].get('processed_audio_seconds') or 0
                            value['asrProcessedSamples'] = min(value['watermarks']['durable'], max(0, round(processed * 16000)))
                            value['asrQueueSeconds'] = execution['runtime'].get('asr_queue_seconds') or 0
                            execution['pendingTail'] = message.get('pending', False)
                        elif kind == 'hypothesis' and not cancel.is_set():
                            audio['temporary'] = dict(text=message['text'], truncated=message['truncated'],
                                confirmed=False, observedWall=message['observedWall'])
                        elif kind == 'complete':
                            asr_complete = message['complete']
                            execution['result'] = message.get('evidence')
                        elif kind == 'released':
                            execution['release'] = message['evidence']
                        elif kind == 'asr_failed':
                            audio['error'] = '识别中断，录音仍保留。' + message['message']
                            execution['error'] = message['message']
                            value['reason'] = 'asr_failed'
                            if stopping_at is None:
                                process.control('stop', 'asr_failed')
                                stopping_at = now
                                control_wait = now
                        elif kind == 'asr_ended':
                            asr_done = True
                    if translator and not translator.is_alive():
                        translator.join()
                        cue_id, revision, result, error = translation_result.get_nowait()
                        cue = next((c for c in audio['cues'] if c['id'] == cue_id), None)
                        if cue is None:
                            if error:
                                audio['error'] = '翻译模型准备失败，录音和原文继续保存。' + error
                                translation_halted = True
                            else:
                                model_ready_at = time.monotonic()
                        elif cue['revision'] == revision:
                            if result and not cancel.is_set():
                                model_ready_at = time.monotonic()
                                proposed = deepcopy(self.task)
                                candidate = next(c for c in proposed['audio']['cues'] if c['id'] == cue_id)
                                candidate.update(state='completed', result=result, translationRevision=revision)
                                validate_audio_task(proposed)
                                cue.update(state='completed', result=result, translationRevision=revision)
                            else:
                                cue.update(state='stopped' if cancel.is_set() else 'failed', error=error or '翻译已取消。')
                                translation_halted = True
                            water = 0
                            for translated in audio['cues']:
                                if translated['state'] != 'completed':
                                    break
                                water = max(water, translated['endSample'])
                            value['watermarks']['translated'] = water
                            self._persist(changed)
                        translator = None
                    pending_cues = [c for c in audio['cues'] if c['state'] == 'pending' and trim(c['source'])]
                    if not translator and pending_cues and not translation_halted and not cancel.is_set():
                        cue = pending_cues[0]
                        cue['state'] = 'processing'
                        self._persist(changed)
                        translator = threading.Thread(target=translate, args=(deepcopy(cue),), name='capture-translation', daemon=False)
                        translator.start()
                    elif (not translator and not capture_done and not translation_halted and
                            not cancel.is_set() and hasattr(self.engine, 'prepare') and now-model_ready_at>=240):
                        translator = threading.Thread(target=translate, args=(None,), name='capture-model-readiness', daemon=False)
                        translator.start()
                    backlog = value['watermarks']['durable'] - value.get('asrProcessedSamples', 0)
                    value['backlogPeakSamples'] = max(value.get('backlogPeakSamples', 0), backlog)
                    if (backlog > 120 * 16000 or len(pending_cues) > 128) and stopping_at is None:
                        process.control('stop', 'inference_backlog_limit')
                        stopping_at = now
                        control_wait = now
                        value['reason'] = 'inference_backlog_limit'
                    if capture_done and not translator and (asr_done or not asr_thread):
                        break
            with self.lock:
                audio['asrComplete'] = bool(asr_complete and not cancel.is_set())
                execution['status'] = 'failed' if execution.get('error') else 'completed' if audio['asrComplete'] else 'stopped'
                if audio['asrComplete']:
                    audio['checkpoint'] = dict(sample=value['watermarks']['durable'], cueCount=len(audio['cues']))
                    value['watermarks']['asrConsumed'] = value['watermarks']['durable']
                    value['asrProcessedSamples'] = value['watermarks']['durable']
                    audio.pop('temporary', None)
                self._seal_recording(changed)
            return 'cancelled' if cancel.is_set() else 'completed' if audio['asrComplete'] and not translation_halted else 'partial'
        except (Cancelled, CoreError, OSError, ValueError, queue.Full) as exc:
            failure = str(exc)
            if process:
                try:
                    process.control('stop', 'capture_failure')
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not capture_done:
                        event = process.next_event(.05)
                        if event and not self.save_blocked:
                            with self.lock:
                                capture_event(event)
                        if process.ended():
                            break
                except (CoreError, OSError, ValueError):
                    pass
            with self.lock:
                value['reason'] = failure
                value['state'] = 'save_blocked' if self.save_blocked else 'interrupted'
                audio.update(phase=value['state'], error=failure)
                execution.update(status='failed', error=failure)
                if not self.save_blocked:
                    try:
                        flush()
                        self._persist(changed)
                        self._feed()
                        self._seal_recording(changed)
                    except (CoreError, OSError) as seal_error:
                        self.warning = '录音封存未确认，请保留分块并主动恢复。' + str(seal_error)
                        self.save_blocked = True
                changed(self.snapshot())
            return 'cancelled' if isinstance(exc, Cancelled) else 'failed'
        finally:
            asr_cancel.set()
            if self.engine:
                self.engine.cancel()
            if process:
                process.close()
            if asr_thread:
                asr_thread.join(timeout=12)
            if translator:
                translator.join(timeout=12)
            with self.lock:
                value['terminal'] = dict(elapsedSeconds=time.monotonic() - start, error=failure,
                    asrThreadEnded=not asr_thread or not asr_thread.is_alive(),
                    translationThreadEnded=not translator or not translator.is_alive())
                for cue in audio['cues']:
                    if cue['state'] == 'processing':
                        cue.update(state='stopped', error='本次翻译未确认，等待主动补译。')
                self.engine = None
                self.active = None
                self.capture_runtime = None
                if not self.save_blocked:
                    self._persist(changed)

