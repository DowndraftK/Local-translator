"""Mac audio facts in owned subprocesses. The shared worker stays GPU-free."""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import uuid

from . import CoreError, Cancelled
from .audio import MAX_SOURCE_BYTES
from .resources import atomic_json, no_links, encoded, parse_binding, token
from .resource_macos import environment, supported_speech_route, trusted_manifest, whisper_variant
from .store import atomic_write, file_identity, safe_file


class MacAudio:
    supports_prepared_live = True
    def __init__(self, root, playback_helper=None):
        self.root = Path(root); no_links(self.root); self.root.mkdir(parents=True, exist_ok=True)
        self.playback_helper = playback_helper
        self.lock = threading.Lock(); self.player = None; self.session = None
        self.live_host = None

    @property
    def identity(self):
        names = ('audio.py', 'audio_tasks.py', 'audio_macos.py', 'audio_macos_helper.py',
                 'capture.py', 'capture_tasks.py', 'capture_macos.py', 'capture_speech.py',
                 'audio_live_host.py', 'live_warmup.wav')
        return hashlib.sha256(encoded({n: hashlib.sha256(safe_file(Path(__file__).with_name(n))).hexdigest() for n in names})).hexdigest()

    @contextmanager
    def translation_turn(self, path, cancel):
        import fcntl
        pending = path.with_name('capture-translation.pending')
        atomic_write(b'', pending)
        try:
            with path.open('rb') as lock:
                deadline = time.monotonic() + 180
                while True:
                    if cancel.is_set(): raise Cancelled()
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise CoreError('等待识别GPU执行权超过时限，字幕保留待补译。', 'capture_inference_timeout')
                        cancel.wait(.03)
                yield
        finally:
            pending.unlink(missing_ok=True)

    def execute(self, stage, source, binding, cancel, receive, **fields):
        binding = parse_binding(binding); resources = binding['resources']
        for name, value in resources.items():
            if not trusted_manifest(name, value['manifest']['sha256']):
                raise CoreError('音频执行资源不是已审核的固定组合。', 'audio_binding')
        runtime = Path(resources['speech.runtime']['root'])
        folder = self.root / uuid.uuid4().hex; folder.mkdir()
        request = dict(stage=stage, source=str(source), folder=str(folder), parent_pid=os.getpid(), **fields)
        if stage != 'decode':
            model_pin = resources['speech.model']['manifest']['sha256']
            if not supported_speech_route(model_pin, resources['speech.bridge']['manifest']['sha256']):
                raise CoreError('语音模型与当前识别程序版本不匹配，请重新选择资源。', 'audio_binding')
            request.update(bridge=resources['speech.bridge']['root'],
                model_revision=json.loads(Path(resources['speech.model']['manifest']['path']).read_bytes())['revision'],
                config=dict(source=str(runtime / 'Code/source'), source_manifest=str(runtime / 'Code/pinned-source.json'),
                    model=resources['speech.model']['root'], model_manifest=resources['speech.model']['manifest']['path'],
                    device='mps', dtype='float32', max_context_tokens=128,
                    language=binding['execution']['language'], model_variant=whisper_variant(model_pin)))
        else: request['output'] = str(folder / 'audio.wav')
        if stage == 'live':
            request['config']['vad_preroll_seconds'] = .25
            if binding['execution']['language']=='en':
                request['config']['live_warmup_file'] = str(Path(__file__).with_name('live_warmup.wav'))
            request['latency_profile'] = 't08-prepared-live-v1'
        atomic_json(folder / 'request.json', request)
        if stage == 'live':
            from .audio_live_host import LiveSpeechHost
            binding_key = hashlib.sha256(encoded(dict(resources=resources, config=request['config']))).hexdigest()
            if self.live_host and (self.live_host.binding_key != binding_key or
                    self.live_host.closed or self.live_host.process.poll() is not None):
                self.close()
            if self.live_host is None:
                self.live_host = LiveSpeechHost(self.root, runtime, resources['speech.assets']['root'],
                    binding_key, self.identity)
            self.live_host.execute(request, folder, cancel, receive)
            return folder
        self.close()
        env = environment(folder); env['LOCAL_TRANSLATOR_RUNTIME_ROOT'] = str(runtime)
        if stage != 'decode': env['LOCAL_TRANSLATOR_ASSETS_ROOT'] = resources['speech.assets']['root']
        python = runtime / 'Python/bin/python3.12'
        helper = Path(__file__).with_name('audio_macos_helper.py')
        # Fixed resources are read-only, all new writes are confined to this execution.
        escaped = str(folder).replace('\\', '\\\\').replace('"', '\\"')
        profile = '(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath "' + escaped + '") (literal "/dev/null"))'
        from .mac_sandbox import sandbox_command
        args = sandbox_command(profile, [python, '-I', '-B', helper, folder / 'request.json'], folder)
        self._run(args, folder, env, cancel, receive, failure_code='audio_decode' if stage == 'decode' else 'audio_process',
                  timeout=3000 if stage == 'live' else 600)
        return folder

    def close(self):
        if self.live_host is not None:
            self.live_host.close()
            self.live_host = None

    def _run(self, args, folder, env, cancel, receive, playback=False, failure_code='audio_process', timeout=600):
        child = subprocess.Popen(args, cwd=folder, env=env, stdin=subprocess.PIPE if playback else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        atomic_json(folder / 'owner.json', dict(pid=child.pid, parent_pid=os.getpid(), argv=args,
            started_at=time.time(), timeout_seconds=timeout, adapter=self.identity, stage='playback' if playback else 'audio'))
        if playback:
            with self.lock: self.player = child
        messages = queue.Queue(maxsize=128); overflow = threading.Event(); errors = []
        def reader(pipe, name, parse):
            count = 0
            try:
                with pipe, (folder / name).open('xb') as log:
                    while line := pipe.readline(65537):
                        count += len(line)
                        if len(line) > 65536 or count > 16 * 1024 * 1024:
                            overflow.set(); break
                        log.write(line); log.flush()
                        if parse:
                            value = json.loads(line)
                            try: messages.put(value, timeout=1)
                            except queue.Full: overflow.set(); break
            except BaseException as exc: errors.append(str(exc)); overflow.set()
        readers = [threading.Thread(target=reader, args=(child.stdout, 'stdout.jsonl', True)),
                   threading.Thread(target=reader, args=(child.stderr, 'stderr', False))]
        for thread in readers: thread.start()
        started = time.monotonic(); cancelled_at = None; forced = False; terminal = False; loading_at = None
        try:
            while child.poll() is None or any(t.is_alive() for t in readers) or not messages.empty():
                if cancel.is_set() and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    if child.poll() is None:
                        if playback:
                            try: self.control('stop', self.session)
                            except CoreError: pass
                        else: child.send_signal(signal.SIGINT)
                elapsed = time.monotonic() - started
                if elapsed > timeout: raise CoreError('音频阶段达到已登记时限，请保留已保存结果。', 'audio_timeout')
                if loading_at and time.monotonic() - loading_at > 180:
                    raise CoreError('语音模型加载超过180秒，录音前缀保留。', 'audio_timeout')
                if overflow.is_set(): raise CoreError('音频事实流损坏、积压或超限。' + str(errors), 'audio_process')
                if cancelled_at is not None and child.poll() is None:
                    wait = time.monotonic() - cancelled_at
                    if wait > 7: child.kill(); forced = True
                    elif wait > 5: child.terminate()
                try: event = messages.get(timeout=.04)
                except queue.Empty: continue
                if timeout > 600:
                    if event.get('event') == 'state' and event.get('state') == 'loading': loading_at = time.monotonic()
                    if event.get('event') == 'ready': loading_at = None
                if event.get('event') == 'failure' or playback and event.get('event') == 'error':
                    message = event.get('message', '音频后端失败。')
                    code = message.split(':', 1)[0]
                    if code not in ('audio_format', 'audio_empty', 'audio_limit', 'audio_decode', 'audio_incomplete'):
                        code = 'audio_playback' if playback else failure_code
                    raise CoreError(message, code)
                if event.get('event') in ('stopped', 'complete', 'decoded'):
                    terminal = True
                if not cancel.is_set(): receive(event, folder)
            if cancel.is_set(): raise Cancelled()
            if not terminal: raise CoreError('音频后端缺少完整结束标记。', 'audio_incomplete')
            if child.returncode != 0: raise CoreError('音频后端异常结束，请查看执行详情。', 'audio_process')
        finally:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=1)
                except subprocess.TimeoutExpired: child.kill(); forced = True
            child.wait(timeout=2)
            if child.stdin: child.stdin.close()
            for thread in readers: thread.join(timeout=2)
            from .mac_sandbox import release_temporary
            temporary_released = release_temporary(folder, child.pid)
            with self.lock:
                if self.player is child: self.player = None; self.session = None
            atomic_json(folder / 'terminal.json', dict(pid=child.pid, OS_exit=child.returncode, forced=forced,
                cancelled=cancel.is_set(), readers_drained=not any(t.is_alive() for t in readers),
                elapsed_seconds=time.monotonic() - started, terminal_seen=terminal,
                temporary_released=temporary_released))

    def play(self, source, cancel, receive, session, start_sample=0):
        if not self.playback_helper or not Path(self.playback_helper).is_file():
            raise CoreError('当前Mac播放器helper缺失。', 'audio_playback')
        no_links(Path(self.playback_helper).absolute())
        folder = self.root / uuid.uuid4().hex; folder.mkdir()
        with self.lock: self.session = session
        original = token(source)
        def event(value, folder):
            if value.get('session') != session: raise CoreError('播放响应归属错误。', 'audio_playback')
            if token(source) != original: raise CoreError('播放音频被外部替换。', 'source_mismatch')
            receive(value, folder)
        return self._run([str(self.playback_helper), str(source), session, str(start_sample / 16000)], folder, environment(folder), cancel, event, True)

    def control(self, action, session, seconds=None):
        with self.lock:
            if not self.player or self.session != session or self.player.poll() is not None:
                raise CoreError('播放会话已结束或归属改变。', 'audio_playback')
            value = dict(action=action, session=session)
            if seconds is not None: value['seconds'] = seconds
            try: self.player.stdin.write(encoded(value) + b'\n'); self.player.stdin.flush()
            except (OSError, ValueError) as exc: raise CoreError('播放器连接已结束。', 'audio_playback') from exc
