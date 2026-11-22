"""Qt parent process: confirm readiness, bind results, then drain before exit."""
import json
import base64
import hashlib
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal, Qt

from .protocol import (MAX_FRAME_BYTES, STARTUP_TIMEOUT_MS, STARTUP_ERROR_PREFIX, SHUTDOWN_TIMEOUT_MS,
                       MAX_TRANSFER_BYTES, SNAPSHOT_CHUNK_BYTES, EventTracker, ProtocolError, WORKBENCH_OPERATIONS, encode, request)
from .runtime import owned_environment, owned_python


class WorkerClient(QObject):
    event = Signal(dict)
    changed = Signal(str)
    stopped = Signal(int)
    snapshot = Signal(dict)
    model_catalog_changed = Signal()
    speech_catalog_changed = Signal()

    def __init__(self, parent=None, launch_path=None, startup_timeout_ms=STARTUP_TIMEOUT_MS,
                 task_root=None, boundary_helper=None, endpoint=None, pdf_helper=None, pdf_library=None,
                 dictionary_config=None, image_helper=None, preview_data_root=None, install_catalog=None, catalog_sha256=None, translation_models_root=None):
        super().__init__(parent)
        self.launch_path = Path(launch_path or Path(__file__).resolve().parents[1] / "launch.py")
        self.process = None
        self.state = "disconnected"
        self.operation = None
        self.requests = {}
        self.buffer = bytearray()
        self.stderr = bytearray()
        self.diagnostic = None
        self.terminal = None
        self.failure = None
        self.startup_error = None
        self.ready_confirmed = False
        self.shutdown_confirmed = False
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.startup_expired)
        self.timeout_ms = startup_timeout_ms
        self.task_root, self.boundary_helper = task_root, boundary_helper
        self.endpoint = endpoint
        self.pdf_helper, self.pdf_library = pdf_helper, pdf_library
        self.image_helper = image_helper
        self.dictionary_config = dictionary_config
        self.preview_data_root = preview_data_root
        self.install_catalog, self.catalog_sha256 = install_catalog, catalog_sha256
        self.translation_models_root = translation_models_root
        from .resource_client import ResourceClient
        self.resources = ResourceClient(self)
        from .settings_client import SettingsClient
        self.preferences = SettingsClient(self, 'preferences')
        self.managed = SettingsClient(self, 'managed')
        from .dictionary_client import DictionaryClient
        self.dictionary = DictionaryClient(self)
        from .history_client import HistoryClient
        self.history = HistoryClient(self)
        self.current_task = None
        self.model_catalog = []
        self.speech_catalog = None
        self.speech_choices = {}
        self.can_change_task = lambda: True
        self.transfer = None
        self.snapshot_context = None
        self.operation_kind = None
        self.heartbeat = QTimer(self); self.heartbeat.setInterval(5000)
        self.heartbeat.timeout.connect(self.renew_lease)
        self.shutdown_timer = QTimer(self); self.shutdown_timer.setSingleShot(True)
        self.shutdown_timer.timeout.connect(self.force_stop)

    def set_state(self, state):
        self.state = state; self.changed.emit(state)

    def task_request_pending(self):
        """UI dispatch gate for synchronous task mutations, separate from operations."""
        return any((kind.startswith(("task.", "document.", "image.", "audio.", "capture.")) and kind not in ("task.list", "task.export") or kind == "history.update")
                   or kind in ('resource.select','resource.save','resource.discard') or kind.startswith(('preferences.stage','preferences.save','preferences.discard','preferences.reload','preferences.confirm')) for kind in self.requests.values())

    def can_reconnect(self):
        return self.state in ("disconnected", "failed") and (
            self.process is None or self.process.state() == QProcess.NotRunning)

    def connection_error_text(self):
        if not self.failure:
            return ""
        if self.failure == "writer_busy":
            return "另一个本地翻译器窗口正在使用任务库。请退出那个版本，再点「重新检测」。"
        if self.failure == "worker_data_permission":
            return "工作进程无法访问应用数据目录。请检查 macOS 权限后点「重新检测」。"
        if self.failure == "startup_unconfirmed_30s":
            return "等待工作进程就绪超时。请点「重新检测」。"
        if self.startup_error:
            return self.startup_error["message"]
        if self.ready_confirmed:
            return "工作进程已断开。请在偏好设置中点「重新检测」。"
        return "工作进程未能启动。可点「重新检测」或查看连接诊断。"

    def connect_worker(self):
        if self.process:
            if self.process.state() != QProcess.NotRunning:
                return False
            self.process.deleteLater()
        self.session_id = str(uuid.uuid4()); self.tracker = EventTracker(self.session_id)
        self.dictionary.disconnect()
        self.history.disconnect()
        self.resources.disconnect()
        self.preferences.disconnect()
        self.managed.disconnect()
        self.buffer.clear(); self.stderr.clear(); self.requests.clear()
        self.operation = self.terminal = self.failure = self.startup_error = self.diagnostic = self.transfer = None
        self.ready_confirmed = self.shutdown_confirmed = False
        self.current_task = None
        self.snapshot_context = None
        self.set_model_catalog([])
        self.speech_catalog = None
        self.speech_choices = {}
        self.speech_catalog_changed.emit()
        process = QProcess(self); self.process = process
        environment = QProcessEnvironment()
        for key, value in owned_environment().items():
            environment.insert(key, value)
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(self.launch_path.parent))
        process.setProgram(owned_python())
        arguments = ["-I", "-B", "-X", "utf8", str(self.launch_path), "worker", "--session-id", self.session_id]
        if self.task_root: arguments += ["--task-root", str(self.task_root)]
        if self.boundary_helper: arguments += ["--boundary-helper", str(self.boundary_helper)]
        if self.endpoint: arguments += ["--endpoint", self.endpoint]
        if self.pdf_helper: arguments += ["--pdf-helper", str(self.pdf_helper)]
        if self.image_helper: arguments += ["--image-helper", str(self.image_helper)]
        if self.pdf_library: arguments += ["--pdf-library", str(self.pdf_library)]
        if self.dictionary_config: arguments += ["--dictionary-config", str(self.dictionary_config)]
        if self.preview_data_root: arguments += ['--preview-data-root',str(self.preview_data_root)]
        if self.install_catalog: arguments += ['--install-catalog',str(self.install_catalog)]
        if self.catalog_sha256: arguments += ['--catalog-sha256',self.catalog_sha256]
        if self.translation_models_root: arguments += ['--translation-models-root',str(self.translation_models_root)]
        process.setArguments(arguments)
        process.started.connect(lambda: self.send("hello") if process is self.process else None)
        process.readyReadStandardOutput.connect(lambda: self.read_output(process))
        process.readyReadStandardError.connect(lambda: self.read_error(process))
        process.finished.connect(lambda code, status: self.on_exit(process, code))
        process.errorOccurred.connect(lambda error: self.on_process_error(process, error))
        self.set_state("connecting"); self.timer.start(self.timeout_ms); process.start()
        return True

    def send(self, kind, **fields):
        if not self.process or self.process.state() != QProcess.Running:
            return None
        if kind in ('task.create','task.open','task.import','document.create','document.fork','image.create','image.fork','audio.create') and not self.can_change_task():
            return None
        if kind in ("task.save", "task.export", 'document.draft', 'document.apply', 'document.discard', 'document.review', 'document.fork', 'image.draft','image.apply','image.discard','image.review','image.fork','image.retry_save','audio.draft','audio.apply','audio.discard','audio.review','audio.control','audio.retry_save','audio.export_wav') and self.current_task:
            fields.setdefault("task_id", self.current_task["id"])
            fields.setdefault("revision", self.current_task["revision"])
        message = request(kind, self.session_id, **fields)
        if len(self.requests) >= 512:
            self.fail("request_limit"); return None
        self.requests[message["request_id"]] = kind
        self.process.write(encode(message))
        return message["request_id"]

    def set_model_catalog(self, models):
        if models != self.model_catalog:
            self.model_catalog = models
            self.model_catalog_changed.emit()

    def read_error(self, process):
        raw = bytes(process.readAllStandardError())
        if process is self.process:
            # Keep a bounded error tail, never an unbounded product log.
            self.stderr.extend(raw); self.stderr[:] = self.stderr[-4096:]

    def read_output(self, process):
        raw = bytes(process.readAllStandardOutput())
        if process is not self.process:
            return
        self.buffer.extend(raw)
        try:
            while b"\n" in self.buffer:
                end = self.buffer.index(b"\n") + 1
                if end > MAX_FRAME_BYTES:
                    raise ProtocolError("frame_too_large")
                frame = bytes(self.buffer[:end]); del self.buffer[:end]
                self.receive(self.tracker.accept(frame))
            if len(self.buffer) > MAX_FRAME_BYTES:
                raise ProtocolError("frame_too_large")
        except (ProtocolError, KeyError, TypeError, ValueError) as exc:
            self.fail(str(exc))

    def receive(self, event):
        kind, request_kind = event["type"], self.requests.get(event["request_id"])
        if request_kind is None:
            raise ProtocolError("unknown_request_result")
        if request_kind == 'environment.speech_choice':
            self.requests.pop(event['request_id'], None)
            if kind == 'environment.choice':
                self.speech_choices = event['choices']
            self.event.emit(event)
            return
        if request_kind.startswith('environment.') and kind in ('environment.description', 'environment.error'):
            self.requests.pop(event['request_id'], None)
            if request_kind != 'environment.scan' and kind == 'environment.error':
                self.operation = None
                self.heartbeat.stop()
                if self.state != 'closing':
                    self.set_state('ready')
            if kind == 'environment.description':
                services = event['description']['ollama']['external']
                self.set_model_catalog(list(dict.fromkeys(
                    name for service in services for name in service.get('catalog', ()))))
                speech = event['description'].get('speech_presets')
                self.speech_catalog = speech['rows'] if speech else event['description']['whisper'].get('variants')
                if speech:
                    self.speech_choices = speech['choices']
                self.speech_catalog_changed.emit()
            self.event.emit(event)
            return
        if request_kind.startswith(('preferences.', 'managed.')) and kind.startswith(('preferences.', 'managed.')) and kind != 'managed.progress':
            self.requests.pop(event['request_id'], None)
            channel = self.preferences if request_kind.startswith('preferences.') else self.managed
            if request_kind == 'managed.operate' and kind == 'managed.error':
                self.operation = None
                self.heartbeat.stop()
                if self.state != 'closing': self.set_state('ready')
            channel.receive(event, request_kind)
            self.event.emit(event)
            return
        if request_kind.startswith('resource.') and kind.startswith('resource.') and kind != 'resource.progress':
            self.requests.pop(event['request_id'],None)
            if request_kind=='resource.check' and kind=='resource.error':
                self.operation=None; self.heartbeat.stop()
                if self.state!='closing': self.set_state('ready')
            self.resources.receive(event,request_kind); self.event.emit(event); return
        if request_kind.startswith('history.'):
            self.requests.pop(event['request_id'], None)
            self.history.receive(event,request_kind)
            return
        if request_kind.startswith("dictionary."):
            self.dictionary.receive(event, request_kind)
            self.requests.pop(event["request_id"], None)
            return
        if kind == "ready":
            if self.state != "connecting" or request_kind != "hello":
                raise ProtocolError("unexpected_ready")
            self.ready_confirmed = True
            self.timer.stop(); self.diagnostic = event["diagnostics"]; self.set_state("ready")
            self.dictionary.configure(self.diagnostic)
            self.history.configure(self.diagnostic)
            self.resources.configure(self.diagnostic)
            self.preferences.configure(self.diagnostic)
            self.managed.configure(self.diagnostic)
        elif kind in ("operation.started", "audio.playback", "progress", "resource.progress", "managed.progress", "environment.progress", "cancel.accepted", "operation.finished"):
            if not self.operation or (event.get("operation_id"), event.get("lease_id")) != self.operation:
                raise ProtocolError("operation_or_lease_mismatch")
            if request_kind != ("cancel" if kind == "cancel.accepted" else self.operation_kind):
                raise ProtocolError("unexpected_operation_result")
            if kind == "operation.finished":
                if event.get("tail_drained") is not True:
                    raise ProtocolError("tail_not_drained")
                self.terminal = event; self.operation = None; self.heartbeat.stop()
                if request_kind=='resource.check': self.resources.receive(event,request_kind)
                if request_kind=='managed.operate': self.managed.receive(event,request_kind)
                if request_kind.startswith('environment.'):
                    detail = event.get('result', {}).get('detail') or {}
                    if detail.get('dictionary'):
                        self.dictionary.configure(dict(capabilities={'dictionary':'dictionary-query-v1'},
                                                       dictionary=detail['dictionary']))
                    if detail.get('resources'):
                        self.resources.accept(detail['resources'])
                if self.state != "closing":
                    self.set_state("ready")
            elif kind=='resource.progress': self.resources.receive(event,request_kind)
            elif kind=='managed.progress': self.managed.receive(event,request_kind)
        elif kind == "diagnostics" and request_kind == "diagnose":
            self.diagnostic = event["diagnostics"]
        elif kind == "shutdown.confirmed" and request_kind == "shutdown":
            if self.state != "closing" or event.get("tail_drained") is not True:
                raise ProtocolError("unexpected_shutdown")
            self.shutdown_confirmed = True
        elif kind == "cancel.already_finished" and request_kind == "cancel":
            identity = (event.get('operation_id'),event.get('lease_id'))
            recent = (self.terminal['operation_id'],self.terminal['lease_id']) if self.terminal else None
            if identity not in (self.operation,recent):
                raise ProtocolError("unexpected_cancel_completion")
        elif kind.startswith("snapshot."):
            self.receive_snapshot(event)
        elif kind == "lease.renewed" and request_kind == "lease.renew":
            expected = self.operation or ((self.terminal["operation_id"], self.terminal["lease_id"]) if self.terminal else None)
            if expected != (event.get("operation_id"), event.get("lease_id")):
                raise ProtocolError("operation_or_lease_mismatch")
        elif kind == "task.done" and request_kind.startswith(("task.", "document.", "image.", "audio.", "capture.", 'workbench.')):
            if self.transfer: raise ProtocolError("incomplete_snapshot_transfer")
        elif kind == "task.exported" and request_kind in ("task.export", "audio.export_wav"):
            pass
        elif kind == "error":
            if request_kind in WORKBENCH_OPERATIONS or request_kind in ("capture.start", "capture.recover", "audio.select", "audio.asr", "audio.refine", "audio.play", "task.start", "service.check", "document.select", "document.extract", "document.ocr", "document.render", "image.select", "image.ocr", "image.render",'image.render_selection') or request_kind=='audio.create' and self.operation_kind=='audio.create' and self.operation:
                self.operation = None; self.heartbeat.stop(); self.set_state("ready")
        else:
            raise ProtocolError("unexpected_event")
        if kind in ("ready", "diagnostics", "cancel.accepted", "cancel.already_finished", "operation.finished",
                    "shutdown.confirmed", "lease.renewed", "task.done", "task.exported", "error"):
            self.requests.pop(event["request_id"], None)
        self.event.emit(event)

    def receive_snapshot(self, event):
        kind = event["type"]
        if event.get("operation_id") is not None and self.operation != (event.get("operation_id"), event.get("lease_id")):
            raise ProtocolError("operation_or_lease_mismatch")
        if kind == "snapshot.begin":
            count = event.get("byte_count")
            if self.transfer or type(count) is not int or not 0 < count <= MAX_TRANSFER_BYTES:
                raise ProtocolError("invalid_snapshot_transfer")
            self.transfer = dict(id=event["transfer_id"], request=event["request_id"], count=count,
                                 sha=event["sha256"], index=0, raw=bytearray())
            return
        transfer = self.transfer
        if not transfer or (transfer["id"], transfer["request"]) != (event.get("transfer_id"), event["request_id"]):
            raise ProtocolError("unexpected_snapshot_transfer")
        if kind == "snapshot.chunk":
            if event.get("index") != transfer["index"]: raise ProtocolError("snapshot_chunk_order")
            try: data = base64.b64decode(event["data"], validate=True)
            except ValueError as exc: raise ProtocolError("invalid_snapshot_chunk") from exc
            if not 0 < len(data) <= SNAPSHOT_CHUNK_BYTES or len(transfer["raw"]) + len(data) > transfer["count"]:
                raise ProtocolError("snapshot_transfer_overflow")
            transfer["raw"].extend(data); transfer["index"] += 1
        elif kind == "snapshot.end":
            raw = bytes(transfer["raw"])
            if len(raw) != transfer["count"] or hashlib.sha256(raw).hexdigest() != transfer["sha"]:
                raise ProtocolError("snapshot_checksum_mismatch")
            from translator_core.contracts import strict_json
            snapshot = strict_json(raw)
            if not isinstance(snapshot, dict): raise ProtocolError("invalid_snapshot")
            if 'audio_resources' in snapshot:
                self.resources.accept(snapshot['audio_resources'])
            self.history.observe_epoch(snapshot.get('history_epoch'))
            if "task" in snapshot: self.current_task = snapshot["task"]
            self.snapshot_context = dict(session=self.session_id, request=event['request_id'],
                kind=self.requests.get(event['request_id']), generation=event.get('operation_id'))
            self.transfer = None; self.snapshot.emit(snapshot)
        else: raise ProtocolError("unexpected_snapshot_event")

    def start_operation(self, kind, **fields):
        if self.state != "ready" or self.task_request_pending(): return False
        if kind in ('document.select','image.select','audio.select','audio.create','capture.start') and not self.can_change_task(): return False
        if kind in ("capture.recover", "audio.asr", "audio.refine", "audio.play", "task.start", "document.extract", "document.ocr", "document.render", "image.ocr", "image.render") and self.current_task:
            fields.setdefault("task_id", self.current_task["id"])
            fields.setdefault("revision", self.current_task["revision"])
        self.terminal = None; self.operation_kind = kind
        self.operation = (str(uuid.uuid4()), str(uuid.uuid4()))
        self.set_state("running")
        request_id = self.send(kind, operation_id=self.operation[0], lease_id=self.operation[1], lease_ms=20000, **fields)
        if not request_id:
            self.operation = None
            if self.state == 'running':
                self.set_state('disconnected')
            return False
        self.heartbeat.start()
        return request_id

    def renew_lease(self):
        if self.state == "running" and self.operation:
            self.send("lease.renew", operation_id=self.operation[0], lease_id=self.operation[1])

    def start_probe(self, steps=40, interval_ms=100, lease_ms=10000):
        if self.state != "ready" or self.task_request_pending():
            return False
        self.terminal = None
        self.operation_kind = "probe.start"
        self.operation = (str(uuid.uuid4()), str(uuid.uuid4()))
        self.set_state("running")
        self.send("probe.start", operation_id=self.operation[0], lease_id=self.operation[1],
                  steps=steps, interval_ms=interval_ms, lease_ms=lease_ms)
        return True

    def cancel(self):
        if self.state != "running" or not self.operation:
            return False
        self.set_state("cancelling")
        self.heartbeat.stop()
        self.send("cancel", operation_id=self.operation[0], lease_id=self.operation[1])
        return True

    def shutdown(self):
        if not self.process or self.process.state() == QProcess.NotRunning:
            self.stopped.emit(0); return
        self.history.close()
        self.dictionary.close()
        self.timer.stop(); self.set_state("closing")
        self.heartbeat.stop()
        self.send("shutdown"); self.shutdown_timer.start(SHUTDOWN_TIMEOUT_MS)

    def startup_expired(self):
        if self.state == "connecting":
            self.fail("startup_unconfirmed_30s")

    def fail(self, code):
        self.failure = code; self.timer.stop(); self.heartbeat.stop(); self.set_state("failed")
        # No task requests are sent before ready. An unconfirmed console worker
        # has no window to receive Windows WM_CLOSE, so stop this exact process.
        # Confirmed operations retain the separate tail-drain shutdown window.
        if self.process and self.process.state() != QProcess.NotRunning:
            if not self.ready_confirmed:
                self.process.kill()
            else:
                self.process.terminate(); self.shutdown_timer.start(SHUTDOWN_TIMEOUT_MS)

    def force_stop(self):
        if self.process and self.process.state() != QProcess.NotRunning:
            self.failure = self.failure or "shutdown_unconfirmed_tail"
            self.process.kill()

    def on_process_error(self, process, error):
        if process is self.process and error == QProcess.FailedToStart:
            self.failure = "worker_launch_failed"; self.timer.stop()
            self.set_state("failed"); self.stopped.emit(-1)

    def read_startup_error(self):
        for line in reversed(self.stderr.decode("utf-8", errors="replace").splitlines()):
            if not line.startswith(STARTUP_ERROR_PREFIX):
                continue
            try:
                detail = json.loads(line[len(STARTUP_ERROR_PREFIX):])
            except ValueError:
                continue
            if (isinstance(detail, dict) and isinstance(detail.get("code"), str) and detail["code"]
                    and isinstance(detail.get("message"), str) and detail["message"]):
                return detail
        return None

    def on_exit(self, process, code):
        if process is not self.process:
            return
        self.read_output(process); self.read_error(process)
        self.timer.stop(); self.shutdown_timer.stop(); self.heartbeat.stop()
        if not self.ready_confirmed:
            self.startup_error = self.read_startup_error()
            if self.startup_error:
                self.failure = self.failure or self.startup_error["code"]
        if self.buffer or self.operation or self.transfer or code != 0:
            self.failure = self.failure or "worker_exit_without_confirmed_tail"
        if not self.ready_confirmed:
            self.failure = self.failure or "worker_exit_before_ready"
        if not self.shutdown_confirmed:
            self.failure = self.failure or "worker_exit_without_shutdown_confirmation"
        if self.dictionary.inflight:
            self.failure = self.failure or "dictionary_exit_without_terminal"
        self.requests.clear()
        self.operation = self.transfer = None
        self.dictionary.disconnect()
        self.history.disconnect()
        self.resources.disconnect()
        self.preferences.disconnect()
        self.managed.disconnect()
        self.set_state("failed" if self.failure else "disconnected")
        self.stopped.emit(code)
