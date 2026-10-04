"""Model-free lifecycle probe; importing this module never imports GPU libraries."""
import argparse
import hashlib
import os
import platform
import sys
import threading
import time
from collections import deque

from . import PREVIEW_ID, PRODUCT_VERSION
from .protocol import (PROTOCOL, MAX_FRAME_BYTES, MAX_STEPS, ProtocolError,
                       decode, encode, identifier)


def capabilities():
    return {"diagnostics": "ready", "lifecycle_probe": "ready",
            "text_translation": "not_implemented_b02", "task_read_resume": "not_implemented_b02",
            "pdf_txt": "not_implemented_b03", "ocr": "not_implemented_b04",
            "speech_file": "not_implemented_b05", "refinement_playback": "not_implemented_b06",
            "microphone": "not_implemented_b07", "service_unload": "not_implemented_b08",
            "runtime_installer": "not_implemented_b09", "public_download": "not_implemented_b11"}


def diagnostics():
    return {"preview": PREVIEW_ID, "product_version": PRODUCT_VERSION,
            "platform": sys.platform, "machine": platform.machine(),
            "os_release": platform.release(), "python": platform.python_version(),
            "worker_pid": os.getpid(), "isolated_python": bool(sys.flags.isolated),
            "bytecode_disabled": bool(sys.dont_write_bytecode),
            "transport": "anonymous-pipes-jsonl", "models_loaded": False,
            "production_tasks_opened": False, "gpu_verified": False,
            "capabilities": capabilities()}


class Worker:
    def __init__(self, session_id, output):
        self.session_id, self.output = session_id, output
        self.output_lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.sequence = 0
        self.hello = False
        self.seen = set()
        self.seen_order = deque()
        self.operation = None
        self.last_finished = None
        self.thread = None

    def emit(self, kind, req, **fields):
        with self.output_lock:
            self.sequence += 1
            self.output.write(encode(dict(protocol=PROTOCOL, session_id=self.session_id,
                request_id=req["request_id"], sequence=self.sequence, type=kind, **fields)))
            self.output.flush()

    def error(self, req, code):
        self.emit("error", req, error={"code": code, "retryable": False})

    def handle(self, req):
        if req["session_id"] != self.session_id:
            self.error(req, "session_mismatch"); return True
        if req["request_id"] in self.seen:
            self.error(req, "duplicate_request"); return True
        self.seen.add(req["request_id"]); self.seen_order.append(req["request_id"])
        if len(self.seen_order) > 1024:
            self.seen.remove(self.seen_order.popleft())
        kind = req["type"]
        if kind == "hello" and not self.hello:
            # Confirmation is emitted only after the real request loop is entered.
            self.hello = True
            self.emit("ready", req, diagnostics=diagnostics())
        elif not self.hello:
            self.error(req, "handshake_required")
        elif kind == "diagnose":
            self.emit("diagnostics", req, diagnostics=diagnostics())
        elif kind == "probe.start":
            self.start(req)
        elif kind == "cancel":
            with self.operation_lock:
                op = self.operation
                identity = (req.get("operation_id"), req.get("lease_id"))
                if not op and identity == self.last_finished:
                    self.emit("cancel.already_finished", req, operation_id=identity[0], lease_id=identity[1])
                elif not op or identity != (op["id"], op["lease"]):
                    self.error(req, "operation_or_lease_mismatch")
                else:
                    self.emit("cancel.accepted", req, operation_id=op["id"], lease_id=op["lease"])
                    op["cancel"].set()
        elif kind == "shutdown":
            self.drain()
            self.emit("shutdown.confirmed", req, tail_drained=True)
            return False
        else:
            self.error(req, "unsupported_operation")
        return True

    def start(self, req):
        try:
            op_id, lease_id = identifier(req.get("operation_id")), identifier(req.get("lease_id"))
            steps, interval, lease_ms = req.get("steps", 20), req.get("interval_ms", 100), req.get("lease_ms", 10000)
            if (type(steps) is not int or not 1 <= steps <= MAX_STEPS
                    or type(interval) is not int or not 20 <= interval <= 250
                    or type(lease_ms) is not int or not 100 <= lease_ms <= 30000):
                raise ProtocolError("invalid_probe_bounds")
        except ProtocolError as exc:
            self.error(req, str(exc)); return
        with self.operation_lock:
            if self.operation is not None:
                self.error(req, "busy"); return
            op = {"id": op_id, "lease": lease_id, "cancel": threading.Event(),
                  "deadline": time.monotonic() + lease_ms / 1000}
            self.operation = op
            self.emit("operation.started", req, operation_id=op_id, lease_id=lease_id,
                      steps=steps, lease_ms=lease_ms, result_kind="synthetic-lifecycle-probe")
            self.thread = threading.Thread(target=self.run_probe, args=(req, op, steps, interval), daemon=False)
            self.thread.start()

    def run_probe(self, req, op, steps, interval):
        accepted = []
        status = "completed"
        for step in range(1, steps + 1):
            if op["cancel"].wait(interval / 1000):
                status = "cancelled"; break
            if time.monotonic() >= op["deadline"]:
                status = "lease_expired"; break
            accepted.append(step)
            self.emit("progress", req, operation_id=op["id"], lease_id=op["lease"],
                      confirmed=step, total=steps)
        # Probe has no audio/model/DB tail. Preserve all confirmed steps and mark
        # the terminal result only after the finite operation has drained.
        with self.operation_lock:
            self.emit("operation.finished", req, operation_id=op["id"], lease_id=op["lease"],
                      status=status, tail_drained=True,
                      result={"kind": "synthetic-lifecycle-probe", "confirmed_steps": accepted,
                              "sha256": hashlib.sha256(encode({"steps": accepted})).hexdigest(),
                              "persistent_task": False, "inference": False})
            self.last_finished = (op["id"], op["lease"])
            self.operation = None

    def drain(self):
        with self.operation_lock:
            if self.operation:
                self.operation["cancel"].set()
        if self.thread:
            self.thread.join()  # bounded probe: wait wakes immediately; no inference call


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)
    worker = Worker(identifier(args.session_id), sys.stdout.buffer)
    try:
        while raw := sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1):
            try:
                req = decode(raw)
            except ProtocolError as exc:
                print(f"B01 protocol rejected: {exc}", file=sys.stderr, flush=True)
                raise SystemExit(2) from exc
            if not worker.handle(req):
                break
    finally:
        worker.drain()
