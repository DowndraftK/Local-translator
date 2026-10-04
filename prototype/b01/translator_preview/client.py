"""Qt parent process: confirm readiness, bind results, then drain before exit."""
import json
import base64
import hashlib
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal, Qt

from .protocol import (MAX_FRAME_BYTES, STARTUP_TIMEOUT_MS, SHUTDOWN_TIMEOUT_MS, MAX_TRANSFER_BYTES, SNAPSHOT_CHUNK_BYTES,
                       EventTracker, ProtocolError, encode, request)


class WorkerClient(QObject):
    event = Signal(dict)
    changed = Signal(str)
    stopped = Signal(int)
    snapshot = Signal(dict)

    def __init__(self, parent=None, launch_path=None, startup_timeout_ms=STARTUP_TIMEOUT_MS,
                 task_root=None, boundary_helper=None, endpoint=None):
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
        self.ready_confirmed = False
        self.shutdown_confirmed = False
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.startup_expired)
        self.timeout_ms = startup_timeout_ms
        self.task_root, self.boundary_helper = task_root, boundary_helper
        self.endpoint = endpoint
        self.transfer = None
        self.operation_kind = None
        self.heartbeat = QTimer(self); self.heartbeat.setInterval(5000)
        self.heartbeat.timeout.connect(self.renew_lease)
        self.shutdown_timer = QTimer(self); self.shutdown_timer.setSingleShot(True)
        self.shutdown_timer.timeout.connect(self.force_stop)

    def set_state(self, state):
        self.state = state; self.changed.emit(state)

    def connect_worker(self):
        if self.process and self.process.state() != QProcess.NotRunning:
            return False
        self.session_id = str(uuid.uuid4()); self.tracker = EventTracker(self.session_id)
        self.buffer.clear(); self.stderr.clear(); self.requests.clear()
        self.operation = self.terminal = self.failure = self.diagnostic = self.transfer = None
        self.ready_confirmed = self.shutdown_confirmed = False
        process = QProcess(self); self.process = process
        environment = QProcessEnvironment.systemEnvironment()
        for key in environment.keys():
            if key.startswith(("PYTHON", "CONDA", "DYLD", "QT_", "QML")) or key in ("VIRTUAL_ENV", "LD_LIBRARY_PATH"):
                environment.remove(key)
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(self.launch_path.parent))
        process.setProgram(sys.executable)
        arguments = ["-I", "-B", str(self.launch_path), "worker", "--session-id", self.session_id]
        if self.task_root: arguments += ["--task-root", str(self.task_root)]
        if self.boundary_helper: arguments += ["--boundary-helper", str(self.boundary_helper)]
        if self.endpoint: arguments += ["--endpoint", self.endpoint]
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
        message = request(kind, self.session_id, **fields)
        if len(self.requests) >= 512:
            self.fail("request_limit"); return None
        self.requests[message["request_id"]] = kind
        self.process.write(encode(message))
        return message["request_id"]

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
        if kind == "ready":
            if self.state != "connecting" or request_kind != "hello":
                raise ProtocolError("unexpected_ready")
            self.ready_confirmed = True
            self.timer.stop(); self.diagnostic = event["diagnostics"]; self.set_state("ready")
        elif kind in ("operation.started", "progress", "cancel.accepted", "operation.finished"):
            if not self.operation or (event.get("operation_id"), event.get("lease_id")) != self.operation:
                raise ProtocolError("operation_or_lease_mismatch")
            if request_kind != ("cancel" if kind == "cancel.accepted" else self.operation_kind):
                raise ProtocolError("unexpected_operation_result")
            if kind == "operation.finished":
                if event.get("tail_drained") is not True:
                    raise ProtocolError("tail_not_drained")
                self.terminal = event; self.operation = None; self.heartbeat.stop()
                if self.state != "closing":
                    self.set_state("ready")
        elif kind == "diagnostics" and request_kind == "diagnose":
            self.diagnostic = event["diagnostics"]
        elif kind == "shutdown.confirmed" and request_kind == "shutdown":
            if self.state != "closing" or event.get("tail_drained") is not True:
                raise ProtocolError("unexpected_shutdown")
            self.shutdown_confirmed = True
        elif kind == "cancel.already_finished" and request_kind == "cancel":
            if not self.terminal or (event.get("operation_id"), event.get("lease_id")) != (
                    self.terminal["operation_id"], self.terminal["lease_id"]):
                raise ProtocolError("unexpected_cancel_completion")
        elif kind.startswith("snapshot."):
            self.receive_snapshot(event)
        elif kind == "lease.renewed" and request_kind == "lease.renew":
            expected = self.operation or ((self.terminal["operation_id"], self.terminal["lease_id"]) if self.terminal else None)
            if expected != (event.get("operation_id"), event.get("lease_id")):
                raise ProtocolError("operation_or_lease_mismatch")
        elif kind == "task.done" and request_kind.startswith("task."):
            if self.transfer: raise ProtocolError("incomplete_snapshot_transfer")
        elif kind == "task.exported" and request_kind == "task.export":
            pass
        elif kind == "error":
            if request_kind in ("task.start", "service.check"):
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
            self.transfer = None; self.snapshot.emit(snapshot)
        else: raise ProtocolError("unexpected_snapshot_event")

    def start_operation(self, kind, **fields):
        if self.state != "ready": return False
        self.terminal = None; self.operation_kind = kind
        self.operation = (str(uuid.uuid4()), str(uuid.uuid4()))
        self.set_state("running")
        self.send(kind, operation_id=self.operation[0], lease_id=self.operation[1], lease_ms=20000, **fields)
        self.heartbeat.start()
        return True

    def renew_lease(self):
        if self.state == "running" and self.operation:
            self.send("lease.renew", operation_id=self.operation[0], lease_id=self.operation[1])

    def start_probe(self, steps=40, interval_ms=100, lease_ms=10000):
        if self.state != "ready":
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
        self.timer.stop(); self.set_state("closing")
        self.heartbeat.stop()
        self.send("shutdown"); self.shutdown_timer.start(SHUTDOWN_TIMEOUT_MS)

    def startup_expired(self):
        if self.state == "connecting":
            self.fail("startup_unconfirmed_30s")

    def fail(self, code):
        self.failure = code; self.timer.stop(); self.heartbeat.stop(); self.set_state("failed")
        # Only this parent-owned B01 process is stopped; no process-name killing.
        if self.process and self.process.state() != QProcess.NotRunning:
            self.process.terminate(); self.shutdown_timer.start(SHUTDOWN_TIMEOUT_MS)

    def force_stop(self):
        if self.process and self.process.state() != QProcess.NotRunning:
            self.failure = self.failure or "shutdown_unconfirmed_tail"
            self.process.kill()

    def on_process_error(self, process, error):
        if process is self.process and error == QProcess.FailedToStart:
            self.failure = "worker_launch_failed"; self.timer.stop()
            self.set_state("failed"); self.stopped.emit(-1)

    def on_exit(self, process, code):
        if process is not self.process:
            return
        self.read_output(process); self.read_error(process)
        self.timer.stop(); self.shutdown_timer.stop(); self.heartbeat.stop()
        if self.buffer or self.operation or self.transfer or code != 0:
            self.failure = self.failure or "worker_exit_without_confirmed_tail"
        if not self.ready_confirmed:
            self.failure = self.failure or "worker_exit_before_ready"
        if not self.shutdown_confirmed:
            self.failure = self.failure or "worker_exit_without_shutdown_confirmation"
        self.set_state("failed" if self.failure else "disconnected")
        self.stopped.emit(code)
