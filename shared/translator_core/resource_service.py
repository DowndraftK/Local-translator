"""A private Ollama instance owned by one worker and drained at shutdown."""
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import Cancelled, CoreError
from .resources import atomic_json, json_value, no_links, safe_read
from .resource_macos import environment

ENGINE_PIN = 'add45eb02df0252fb888549d277a66d6b551801869140d1c280bf11689e81b77'


class TranslationService:
    def __init__(self, data_root, managed, models_root, *, bundled_source=None):
        self.root = no_links(Path(data_root).absolute())/'services'
        self.root.mkdir(parents=True, exist_ok=True)
        self.managed = managed
        self.models_root = no_links(Path(models_root).absolute()) if models_root else None
        self.bundled_source = bundled_source
        self.downloaded_models = Path(data_root)/'models/ollama'
        self.process = self.folder = self.endpoint = self.engine_root = self.active_models_root = None
        self.lock = threading.Lock()

    def engine_binary(self):
        if self.bundled_source and not self.managed.blocked and not self.managed.active.get('translation-engine'):
            root = self.bundled_source
        else:
            root = self.managed.active_path('translation-engine')
        return no_links(root/'Contents/Resources/ollama')

    def engine_present(self):
        try:
            binary = self.engine_binary()
            return binary.is_file() and os.access(binary, os.X_OK)
        except (OSError, CoreError):
            return False

    def find_model_root(self, model):
        if not self.bundled_source or not model:
            return self.models_root
        from .local_environment import installed_ollama_model
        configured = os.environ.get('OLLAMA_MODELS')
        for root in dict.fromkeys((self.downloaded_models, self.models_root,
                                  Path(configured) if configured else None, Path.home()/'.ollama/models')):
            if root and installed_ollama_model(root, model):
                return root
        return None

    def ensure(self, cancel, model=None):
        with self.lock:
            binary = self.engine_binary()
            engine_root = binary.parents[2]
            models_root = self.find_model_root(model)
            if (self.process and self.process.poll() is None and self.engine_root == engine_root
                    and self.active_models_root == models_root):
                return self.endpoint
            self._close()
            if not models_root or not models_root.is_dir():
                raise CoreError('请在偏好设置的本地资源区选择可用模型，或主动下载对应版本。', 'model_missing')
            hashed = hashlib.sha256()
            with binary.open('rb') as stream:
                for block in iter(lambda:stream.read(1048576), b''):
                    if cancel.is_set():
                        raise Cancelled()
                    hashed.update(block)
            if hashed.hexdigest() != ENGINE_PIN:
                raise CoreError('翻译引擎执行文件与受信任版本不符，请修复。', 'engine_untrusted')
            self.folder = self.root/uuid.uuid4().hex
            self.folder.mkdir()
            request = self.folder/'request.json'
            atomic_json(request, dict(binary=str(binary), models_root=str(models_root),
                                     folder=str(self.folder), parent_pid=os.getpid()))
            helper = Path(__file__).with_name('resource_service_helper.py')
            self.process = subprocess.Popen([sys.executable, '-I', '-B', str(helper), str(request)],
                                            cwd=self.folder, env=environment(self.folder),
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
            atomic_json(self.folder/'supervisor.json', dict(pid=self.process.pid, parent_pid=os.getpid()))
            started = time.monotonic()
            try:
                while time.monotonic()-started < 50:
                    if cancel.is_set():
                        raise Cancelled()
                    if self.process.poll() is not None:
                        raise CoreError('本应用翻译服务启动失败，请查看资源详情。', 'service_start_failed')
                    ready = self.folder/'ready.json'
                    if ready.is_file():
                        value = json_value(safe_read(ready, 4096))
                        if value.get('version') != 'owned-ollama-ready-v1' or value.get('supervisor_pid') != self.process.pid:
                            raise CoreError('翻译服务归属未确认。', 'service_owner')
                        self.endpoint = value['endpoint']
                        self.engine_root = engine_root
                        self.active_models_root = models_root
                        return self.endpoint
                    cancel.wait(.05)
                raise CoreError('本应用翻译服务启动超过50秒。', 'service_timeout')
            except BaseException:
                self._close()
                raise

    def _close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=18)
                except subprocess.TimeoutExpired as exc:
                    raise CoreError('专用翻译服务尚未排空，资源切换受阻。', 'service_drain') from exc
            self.process = self.endpoint = self.engine_root = self.active_models_root = None

    def close(self):
        with self.lock:
            self._close()
