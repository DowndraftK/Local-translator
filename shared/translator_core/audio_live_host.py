"""One worker-owned speech process reused by sequential capture operations."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import uuid

from . import Cancelled, CoreError
from .resource_macos import environment
from .resources import atomic_json, encoded
from .mac_sandbox import sandbox_command, release_temporary


class LiveSpeechHost:
    def __init__(self, root, runtime, assets, binding_key, adapter_identity):
        self.folder = Path(root) / ('host-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.binding_key = binding_key
        self.messages = queue.Queue(maxsize=128)
        self.failed = threading.Event()
        self.current = None
        self.closed = False
        request = dict(stage='live_host', folder=str(self.folder), parent_pid=os.getpid())
        atomic_json(self.folder/'request.json', request)
        helper = Path(__file__).with_name('audio_macos_helper.py')
        escaped = str(Path(root)).replace('\\', '\\\\').replace('"', '\\"')
        profile = ('(version 1)(allow default)(deny network*)(deny file-write*)'
            '(allow file-write* (subpath "' + escaped + '") (literal "/dev/null"))')
        argv = sandbox_command(profile, [Path(runtime)/'Python/bin/python3.12',
                '-I', '-B', helper, self.folder/'request.json'], self.folder)
        env = environment(self.folder)
        env.update(LOCAL_TRANSLATOR_RUNTIME_ROOT=str(runtime), LOCAL_TRANSLATOR_ASSETS_ROOT=str(assets))
        self.process = subprocess.Popen(argv, cwd=self.folder, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        atomic_json(self.folder/'owner.json', dict(pid=self.process.pid, parent_pid=os.getpid(),
            argv=argv, adapter=adapter_identity, stage='live_host', started_at=time.time()))
        self.readers = [threading.Thread(target=self.read, args=(self.process.stdout,'stdout.jsonl',True)),
                        threading.Thread(target=self.read, args=(self.process.stderr,'stderr',False))]
        for reader in self.readers:
            reader.start()

    def read(self, pipe, name, parse):
        total = 0
        try:
            with pipe, (self.folder/name).open('xb') as output:
                while raw := pipe.readline(65537):
                    total += len(raw)
                    if len(raw)>65536 or total>64*1024*1024:
                        raise ValueError('Speech host log limit exceeded')
                    output.write(raw)
                    output.flush()
                    if parse:
                        self.messages.put(json.loads(raw), timeout=1)
        except (OSError, ValueError, queue.Full):
            self.failed.set()

    def execute(self, request, folder, cancel, receive):
        if self.closed or self.process.poll() is not None or self.current:
            raise CoreError('常驻识别器不可用或已有会话。', 'audio_process')
        execution = folder.name
        self.current = execution
        request.update(execution=execution, retain_model=True)
        atomic_json(folder/'request.json', request)
        atomic_json(folder/'owner.json', dict(pid=self.process.pid, parent_pid=os.getpid(),
            host=str(self.folder), stage='live', execution=execution, started_at=time.time()))
        started = time.monotonic()
        cancelling = None
        finished = False
        try:
            self.process.stdin.write(encoded(dict(action='recognize', request=str(folder/'request.json')))+b'\n')
            self.process.stdin.flush()
            with (folder/'stdout.jsonl').open('xb') as output:
                while not finished:
                    if cancel.is_set() and cancelling is None:
                        cancelling = time.monotonic()
                        self.process.send_signal(signal.SIGINT)
                    if cancelling is not None and time.monotonic()-cancelling>7:
                        raise Cancelled()
                    if time.monotonic()-started>3000:
                        raise CoreError('同传识别会话超时。', 'audio_timeout')
                    if self.failed.is_set() or self.process.poll() is not None:
                        raise CoreError('常驻识别器已退出或事实流损坏。', 'audio_process')
                    try:
                        event = self.messages.get(timeout=.04)
                    except queue.Empty:
                        continue
                    if (event.get('event')=='ready' and event.get('execution') is None and
                            event.get('session')==str(folder)):
                        event['execution'] = execution
                    if event.get('execution') != execution:
                        raise CoreError('识别事实不属于当前会话。', 'capture_owner')
                    output.write(encoded(event)+b'\n')
                    output.flush()
                    if event['event']=='live_finished':
                        finished = True
                        if not event['success'] and not cancel.is_set():
                            raise CoreError(event.get('message','识别未完成。'), 'audio_process')
                    elif event['event']=='failure' and not cancel.is_set():
                        # Retain the failure and wait for the matching drain marker.
                        receive(event, folder)
                    elif not cancel.is_set():
                        receive(event, folder)
            if cancel.is_set():
                raise Cancelled()
        finally:
            self.current = None
            if not finished:
                self.close(force=True)
            atomic_json(folder/'terminal.json', dict(pid=self.process.pid, host=str(self.folder),
                session_drained=finished, model_retained=finished and not self.closed,
                cancelled=cancel.is_set(), elapsed_seconds=time.monotonic()-started))

    def close(self, force=False):
        if self.closed:
            return
        self.closed = True
        forced = False
        if self.process.poll() is None:
            try:
                if force:
                    forced = True
                    self.process.terminate()
                else:
                    self.process.stdin.write(b'{"action":"shutdown"}\n')
                    self.process.stdin.flush()
                    self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                forced = True
                self.process.terminate()
            if forced:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        self.process.wait(timeout=2)
        self.process.stdin.close()
        for reader in self.readers:
            reader.join(timeout=2)
        temporary_released = release_temporary(self.folder, self.process.pid)
        atomic_json(self.folder/'terminal.json', dict(pid=self.process.pid, OS_exit=self.process.returncode,
            forced=forced, readers_drained=not any(reader.is_alive() for reader in self.readers),
            temporary_released=temporary_released))
