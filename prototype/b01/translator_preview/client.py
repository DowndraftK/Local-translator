"""Qt parent process: confirm readiness, bind results, then drain before exit."""
import json
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal, Qt

from .protocol import (MAX_FRAME_BYTES, STARTUP_TIMEOUT_MS, SHUTDOWN_TIMEOUT_MS,
                       EventTracker, ProtocolError, encode, request)


class WorkerClient(QObject):
    event = Signal(dict)
    changed = Signal(str)
    stopped = Signal(int)

    def __init__(self, parent=None, launch_path=None, startup_timeout_ms=STARTUP_TIMEOUT_MS):
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
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.startup_expired)
        self.timeout_ms = startup_timeout_ms
        self.shutdown_timer = QTimer(self); self.shutdown_timer.setSingleShot(True)
        self.shutdown_timer.timeout.connect(self.force_stop)

    def set_state(self, state):
        self.state = state; self.changed.emit(state)

    def connect_worker(self):
        if self.process and self.process.state() != QProcess.NotRunning:
            return False
        self.session_id = str(uuid.uuid4()); self.tracker = EventTracker(self.session_id)
        self.buffer.clear(); self.stderr.clear(); self.requests.clear()
        self.operation = self.terminal = self.failure = self.diagnostic = None
        process = QProcess(self); self.process = process
        environment = QProcessEnvironment.systemEnvironment()
        for key in environment.keys():
            if key.startswith(("PYTHON", "CONDA", "DYLD", "QT_", "QML")) or key in ("VIRTUAL_ENV", "LD_LIBRARY_PATH"):
                environment.remove(key)
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(self.launch_path.parent))
        process.setProgram(sys.executable)
        process.setArguments(["-I", "-B", str(self.launch_path), "worker", "--session-id", self.session_id])
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
            self.timer.stop(); self.diagnostic = event["diagnostics"]; self.set_state("ready")
        elif kind in ("operation.started", "progress", "cancel.accepted", "operation.finished"):
            if not self.operation or (event.get("operation_id"), event.get("lease_id")) != self.operation:
                raise ProtocolError("operation_or_lease_mismatch")
            if request_kind != ("cancel" if kind == "cancel.accepted" else "probe.start"):
                raise ProtocolError("unexpected_operation_result")
            if kind == "operation.finished":
                if event.get("tail_drained") is not True:
                    raise ProtocolError("tail_not_drained")
                self.terminal = event; self.operation = None
                if self.state != "closing":
                    self.set_state("ready")
        elif kind == "diagnostics" and request_kind == "diagnose":
            self.diagnostic = event["diagnostics"]
        elif kind == "shutdown.confirmed" and request_kind == "shutdown":
            if self.state != "closing" or event.get("tail_drained") is not True:
                raise ProtocolError("unexpected_shutdown")
        elif kind == "cancel.already_finished" and request_kind == "cancel":
            if not self.terminal or (event.get("operation_id"), event.get("lease_id")) != (
                    self.terminal["operation_id"], self.terminal["lease_id"]):
                raise ProtocolError("unexpected_cancel_completion")
        elif kind == "error":
            raise ProtocolError(event["error"]["code"])
        else:
            raise ProtocolError("unexpected_event")
        self.event.emit(event)

    def start_probe(self, steps=40, interval_ms=100, lease_ms=10000):
        if self.state != "ready":
            return False
        self.terminal = None
        self.operation = (str(uuid.uuid4()), str(uuid.uuid4()))
        self.set_state("running")
        self.send("probe.start", operation_id=self.operation[0], lease_id=self.operation[1],
                  steps=steps, interval_ms=interval_ms, lease_ms=lease_ms)
        return True

    def cancel(self):
        if self.state != "running" or not self.operation:
            return False
        self.set_state("cancelling")
        self.send("cancel", operation_id=self.operation[0], lease_id=self.operation[1])
        return True

    def shutdown(self):
        if not self.process or self.process.state() == QProcess.NotRunning:
            self.stopped.emit(0); return
        self.timer.stop(); self.set_state("closing")
        self.send("shutdown"); self.shutdown_timer.start(SHUTDOWN_TIMEOUT_MS)

    def startup_expired(self):
        if self.state == "connecting":
            self.fail("startup_unconfirmed_30s")

    def fail(self, code):
        self.failure = code; self.timer.stop(); self.set_state("failed")
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
        self.timer.stop(); self.shutdown_timer.stop()
        if self.buffer or self.operation or code != 0:
            self.failure = self.failure or "worker_exit_without_confirmed_tail"
        self.set_state("failed" if self.failure else "disconnected")
        self.stopped.emit(code)
