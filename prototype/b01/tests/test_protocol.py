"""Actual subprocess/Qt lifecycle and frozen compatibility checks, no models."""
import hashlib
import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from translator_preview.protocol import (PROTOCOL, STARTUP_TIMEOUT_MS, EventTracker,
                                         ProtocolError, decode, encode, request)
from translator_preview.fixtures import verify_fixtures

LAUNCH = Path(__file__).resolve().parents[1] / "launch.py"
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/b01"


class Peer:
    def __init__(self):
        self.session = str(uuid.uuid4()); self.events = queue.Queue()
        self.process = subprocess.Popen([sys.executable, "-I", "-B", str(LAUNCH), "worker",
            "--session-id", self.session], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.reader = threading.Thread(target=self.read, daemon=True); self.reader.start()

    def read(self):
        for line in self.process.stdout:
            self.events.put(json.loads(line))

    def send(self, kind, **fields):
        message = request(kind, self.session, **fields)
        self.raw(encode(message)); return message

    def raw(self, data):
        self.process.stdin.write(data); self.process.stdin.flush()

    def next(self, kind=None, timeout=4):
        deadline = time.monotonic() + timeout
        while True:
            event = self.events.get(timeout=max(.01, deadline - time.monotonic()))
            if kind is None or event["type"] == kind:
                return event

    def hello(self):
        self.send("hello"); return self.next("ready")

    def start(self, **fields):
        fields.setdefault("operation_id", str(uuid.uuid4())); fields.setdefault("lease_id", str(uuid.uuid4()))
        self.send("probe.start", **fields)
        return self.next("operation.started")

    def close(self):
        if self.process.poll() is None:
            try:
                self.send("shutdown"); self.process.wait(timeout=5)
            except (BrokenPipeError, ValueError, subprocess.TimeoutExpired):
                self.process.kill(); self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream and not stream.closed: stream.close()


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.peer = Peer(); self.addCleanup(self.peer.close)

    def test_real_handshake_and_diagnostics_without_models(self):
        event = self.peer.hello(); d = event["diagnostics"]
        self.assertTrue(d["isolated_python"] and d["bytecode_disabled"])
        self.assertEqual(d["worker_pid"], self.peer.process.pid)
        self.assertFalse(d["models_loaded"] or d["production_tasks_opened"] or d["gpu_verified"])
        self.assertEqual(d["capabilities"]["text_translation"], "requires_verified_platform_adapter")
        self.peer.send("diagnose"); self.assertEqual(self.peer.next("diagnostics")["diagnostics"], d)

    def test_handshake_required(self):
        self.peer.send("diagnose")
        self.assertEqual(self.peer.next()["error"]["code"], "handshake_required")

    def test_wrong_process_session_rejected(self):
        message = request("hello", str(uuid.uuid4())); self.peer.raw(encode(message))
        self.assertEqual(self.peer.next()["error"]["code"], "session_mismatch")
        self.peer.hello()

    def test_future_protocol_fails_closed(self):
        message = request("hello", self.peer.session); message["protocol"] = {"major": 99, "minor": 0}
        self.peer.raw(encode(message)); self.assertEqual(self.peer.process.wait(timeout=5), 2)

    def test_frame_limit_fails_closed(self):
        self.peer.raw(b"x" * 65537 + b"\n"); self.assertEqual(self.peer.process.wait(timeout=5), 2)

    def test_success_result_has_operation_lease_and_exact_prefix(self):
        self.peer.hello(); op = self.peer.start(steps=3, interval_ms=20)
        result = self.peer.next("operation.finished")
        self.assertEqual(result["operation_id"], op["operation_id"])
        self.assertEqual(result["lease_id"], op["lease_id"])
        self.assertEqual(result["result"]["confirmed_steps"], [1, 2, 3])
        self.assertTrue(result["tail_drained"])
        self.assertEqual(result["result"]["sha256"], hashlib.sha256(encode({"steps": [1, 2, 3]})).hexdigest())

    def test_cancel_preserves_every_confirmed_step_before_terminal(self):
        self.peer.hello(); op = self.peer.start(steps=40, interval_ms=100)
        first = self.peer.next("progress")
        self.peer.send("cancel", operation_id=op["operation_id"], lease_id=op["lease_id"])
        confirmed = [first["confirmed"]]; ack = False
        while True:
            event = self.peer.next()
            if event["type"] == "progress": confirmed.append(event["confirmed"])
            if event["type"] == "cancel.accepted": ack = True
            if event["type"] == "operation.finished": break
        self.assertTrue(ack and event["tail_drained"])
        self.assertEqual(event["status"], "cancelled")
        self.assertEqual(event["result"]["confirmed_steps"], confirmed)
        self.assertFalse(event["result"]["persistent_task"] or event["result"]["inference"])
        self.peer.send("diagnose"); self.peer.next("diagnostics")

    def test_wrong_lease_cannot_cancel_current_operation(self):
        self.peer.hello(); op = self.peer.start(steps=3, interval_ms=100)
        self.peer.send("cancel", operation_id=op["operation_id"], lease_id=str(uuid.uuid4()))
        self.assertEqual(self.peer.next("error")["error"]["code"], "operation_or_lease_mismatch")
        self.assertEqual(self.peer.next("operation.finished")["status"], "completed")

    def test_cancel_racing_after_completed_tail_is_idempotent(self):
        self.peer.hello(); op = self.peer.start(steps=1, interval_ms=20)
        self.peer.next("operation.finished")
        self.peer.send("cancel", operation_id=op["operation_id"], lease_id=op["lease_id"])
        self.assertEqual(self.peer.next()["type"], "cancel.already_finished")

    def test_busy_rejects_second_operation(self):
        self.peer.hello(); self.peer.start(steps=3, interval_ms=100)
        self.peer.send("probe.start", operation_id=str(uuid.uuid4()), lease_id=str(uuid.uuid4()))
        self.assertEqual(self.peer.next("error")["error"]["code"], "busy")
        self.peer.next("operation.finished")

    def test_lease_expiry_drains_and_reports_explicitly(self):
        self.peer.hello(); self.peer.start(steps=40, interval_ms=100, lease_ms=100)
        result = self.peer.next("operation.finished")
        self.assertEqual(result["status"], "lease_expired"); self.assertTrue(result["tail_drained"])

    def test_duplicate_request_is_not_executed(self):
        message = self.peer.send("hello"); self.peer.next("ready")
        self.peer.raw(encode(message)); self.assertEqual(self.peer.next()["error"]["code"], "duplicate_request")

    def test_eof_drains_owned_probe_and_exits_normally(self):
        self.peer.hello(); self.peer.start(steps=40, interval_ms=100)
        self.peer.process.stdin.close()
        self.assertEqual(self.peer.next("operation.finished")["status"], "cancelled")
        self.assertEqual(self.peer.process.wait(timeout=5), 0)

    def test_shutdown_confirms_after_operation_tail(self):
        self.peer.hello(); self.peer.start(steps=40, interval_ms=100)
        self.peer.send("shutdown")
        terminal = self.peer.next("operation.finished"); end = self.peer.next("shutdown.confirmed")
        self.assertTrue(terminal["tail_drained"] and end["tail_drained"])
        self.assertLess(terminal["sequence"], end["sequence"])
        self.assertEqual(self.peer.process.wait(timeout=5), 0)

    def test_unimplemented_translation_is_not_a_probe(self):
        self.peer.hello(); self.peer.send("text.translate")
        self.assertEqual(self.peer.next()["error"]["code"], "unsupported_operation")


class ContractTests(unittest.TestCase):
    def test_old_session_and_out_of_order_callbacks_rejected(self):
        session = str(uuid.uuid4()); tracker = EventTracker(session)
        event = request("ready", session); event["sequence"] = 1
        tracker.accept(encode(event))
        with self.assertRaises(ProtocolError): tracker.accept(encode(event))
        event["sequence"] = 2; event["session_id"] = str(uuid.uuid4())
        with self.assertRaises(ProtocolError): tracker.accept(encode(event))

    def test_nonfinite_and_boolean_protocol_rejected(self):
        with self.assertRaises(ProtocolError): encode({"bad": float("nan")})
        event = request("hello", str(uuid.uuid4())); event["protocol"] = {"major": True, "minor": 0}
        with self.assertRaises(ProtocolError): decode(encode(event))

    def test_frozen_swift_and_python_binding_dialects_read_without_write(self):
        before = {str(p): p.read_bytes() for p in FIXTURES.rglob("*") if p.is_file()}
        result = verify_fixtures(FIXTURES)
        self.assertEqual(result, {"swift_recipes": 4, "snapshots": 2, "subtitle_recipes": 2, "writes": 0, "inference": False})
        self.assertEqual(before, {str(p): p.read_bytes() for p in FIXTURES.rglob("*") if p.is_file()})


class QtClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def wait(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition(): return
            time.sleep(.005)
        self.fail("Qt client condition did not become true")

    def test_qt_client_start_cancel_reconnect_and_shutdown(self):
        from translator_preview.client import WorkerClient
        client = WorkerClient(); events = []; client.event.connect(events.append)
        self.assertTrue(client.connect_worker()); self.assertFalse(client.connect_worker())
        self.wait(lambda: client.state == "ready")
        self.assertTrue(client.start_probe()); self.wait(lambda: any(x["type"] == "progress" for x in events))
        self.assertTrue(client.cancel()); self.assertFalse(client.cancel())
        self.wait(lambda: client.terminal is not None)
        self.assertEqual(client.terminal["status"], "cancelled")
        session = client.session_id; client.shutdown(); self.wait(lambda: client.state == "disconnected")
        self.assertTrue(client.connect_worker()); self.wait(lambda: client.state == "ready")
        self.assertNotEqual(client.session_id, session)
        client.shutdown(); self.wait(lambda: client.state == "disconnected")
        self.assertIsNone(client.failure)

    def test_qt_client_real_thirty_second_unconfirmed_start_is_bounded(self):
        from translator_preview.client import WorkerClient
        from PySide6.QtCore import QProcess
        self.assertEqual(STARTUP_TIMEOUT_MS, 30000)
        with tempfile.TemporaryDirectory(prefix="b01-unconfirmed-") as temporary:
            stub = Path(temporary) / "unconfirmed.py"; stub.write_text("import time\ntime.sleep(60)\n")
            client = WorkerClient(launch_path=stub); start = time.monotonic()
            client.connect_worker()
            self.wait(lambda: client.state == "failed" and client.process.state() == QProcess.NotRunning, seconds=34)
            elapsed = time.monotonic() - start
            self.assertGreater(elapsed, 28); self.assertLess(elapsed, 34)
            self.assertEqual(client.failure, "startup_unconfirmed_30s")

    def test_qt_client_exit_zero_before_ready_is_failure(self):
        from translator_preview.client import WorkerClient
        from PySide6.QtCore import QProcess
        with tempfile.TemporaryDirectory(prefix="b01-no-ready-") as temporary:
            stub = Path(temporary) / "no_ready.py"; stub.write_text("pass\n")
            client = WorkerClient(launch_path=stub); client.connect_worker()
            self.wait(lambda: client.process.state() == QProcess.NotRunning)
            self.assertEqual(client.state, "failed")
            self.assertEqual(client.failure, "worker_exit_before_ready")

    def test_qt_client_exit_zero_without_shutdown_ack_is_failure(self):
        from translator_preview.client import WorkerClient
        from PySide6.QtCore import QProcess
        with tempfile.TemporaryDirectory(prefix="b01-no-shutdown-ack-") as temporary:
            stub = Path(temporary) / "no_shutdown_ack.py"
            stub.write_text("import json, sys\n"
                "hello = json.loads(sys.stdin.readline())\n"
                "hello.update(type='ready', sequence=1, diagnostics={})\n"
                "print(json.dumps(hello), flush=True)\n"
                "sys.stdin.readline()\n")
            client = WorkerClient(launch_path=stub); client.connect_worker()
            self.wait(lambda: client.state == "ready")
            client.shutdown(); self.wait(lambda: client.process.state() == QProcess.NotRunning)
            self.assertEqual(client.state, "failed")
            self.assertEqual(client.failure, "worker_exit_without_shutdown_confirmation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
