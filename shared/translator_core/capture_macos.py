"""Owned Mac capture process, bounded facts and controls, no task authority."""
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from . import CoreError
from .resources import atomic_json, encoded, no_links
from .resource_macos import environment
from .store import safe_file


class CaptureProcess:
    def __init__(self, helper, folder, request):
        self.folder = folder
        self.messages = queue.Queue(maxsize=128)
        self.overflow = threading.Event()
        self.session = request['session']
        self.started = time.monotonic()
        self.lock = threading.Lock()
        atomic_json(folder / 'request.json', request)
        self.child = subprocess.Popen([str(helper), str(folder / 'request.json')], cwd=folder,
            env=environment(folder), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        atomic_json(folder / 'owner.json', dict(pid=self.child.pid, parent_pid=os.getpid(), argv=self.child.args,
                    at=time.time(), source=request.get('kind'), session=self.session, stage='capture', wall_timeout=2400))
        self.readers = [threading.Thread(target=self.read, args=(self.child.stdout, 'stdout.jsonl', True)),
                        threading.Thread(target=self.read, args=(self.child.stderr, 'stderr', False))]
        for reader in self.readers:
            reader.start()

    def read(self, pipe, name, parse):
        count = 0
        try:
            with pipe, (self.folder / name).open('xb') as log:
                while line := pipe.readline(65537):
                    count += len(line)
                    if len(line) > 65536 or count > (96 * 1024 * 1024 if parse else 1024 * 1024):
                        raise ValueError('采集日志或单帧超过容量。')
                    log.write(line)
                    log.flush()
                    if parse:
                        value = json.loads(line)
                        if not isinstance(value, dict) or value.get('session') != self.session:
                            raise ValueError('采集事实归属无效。')
                        self.messages.put(value, timeout=1)
        except (OSError, ValueError, queue.Full):
            self.overflow.set()

    def next_event(self, timeout):
        if self.overflow.is_set():
            raise CoreError('采集事实流超限、损坏或积压。', 'capture_backpressure')
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def ended(self):
        return self.child.poll() is not None and self.messages.empty() and not any(r.is_alive() for r in self.readers)

    def control(self, action, control):
        with self.lock:
            if self.child.poll() is not None:
                return
            try:
                self.child.stdin.write(encoded(dict(action=action, control=control, session=self.session)) + b'\n')
                self.child.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CoreError('采集控制管道已结束。', 'capture_process') from exc

    def close(self):
        forced = False
        if self.child.poll() is None:
            try:
                self.control('stop', 'owner_release')
                self.child.wait(timeout=10)
            except (CoreError, subprocess.TimeoutExpired):
                self.child.terminate()
                try:
                    self.child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    forced = True
                    self.child.kill()
        self.child.wait(timeout=2)
        self.child.stdin.close()
        # Readers may be waiting on the bounded queue after the owner stopped
        # consuming. Drain factual frames without crediting them to TaskStore.
        deadline = time.monotonic() + 2
        while any(r.is_alive() for r in self.readers) and time.monotonic() < deadline:
            try:
                self.messages.get(timeout=.05)
            except queue.Empty:
                pass
        for reader in self.readers:
            reader.join(timeout=1)
        atomic_json(self.folder / 'terminal.json', dict(pid=self.child.pid, OS_exit=self.child.returncode,
            forced=forced, readers_drained=not any(r.is_alive() for r in self.readers),
            overflow=self.overflow.is_set(), elapsed_seconds=time.monotonic() - self.started))


class MacCapture:
    def __init__(self, root, helper):
        self.root = Path(root)
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.helper = Path(helper)

    @property
    def identity(self):
        return hashlib.sha256(safe_file(self.helper, 8 * 1024 * 1024)).hexdigest()

    def process(self, request):
        no_links(self.helper)
        folder = self.root / uuid.uuid4().hex
        folder.mkdir()
        return CaptureProcess(self.helper, folder, request)

    def describe(self):
        child = self.process(dict(action='describe', session=str(uuid.uuid4())))
        try:
            event = child.next_event(10)
            if not event or event.get('event') != 'description':
                raise CoreError('设备或权限查询未确认。', 'capture_unavailable')
            return event
        finally:
            child.close()

    def start(self, source, session):
        return self.process(dict(action='start', session=session, kind=source['kind'],
            deviceUID=source.get('deviceUID'), excludedPIDs=[os.getpid(), os.getppid()],
            selectedSource=source))
