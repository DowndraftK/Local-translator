"""Model-free lifecycle probe; importing this module never imports GPU libraries."""
import argparse
import base64
import hashlib
import json
import os
import platform
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from translator_core import CORE_VERSION, CoreError
from translator_core.contracts import default_recipe, sha
from translator_core.ollama import Ollama
from translator_core.planning import MacBoundaries
from translator_core.store import TaskStore, atomic_write
from translator_core.tasks import TextTasks

from . import PREVIEW_ID, PRODUCT_VERSION
from .protocol import (PROTOCOL, MAX_FRAME_BYTES, MAX_STEPS, SNAPSHOT_CHUNK_BYTES, ProtocolError,
                       decode, encode, identifier)


def capabilities(configured=False):
    return {"diagnostics": "ready", "lifecycle_probe": "ready",
            "text_translation": "ready" if configured and sys.platform == "darwin" else "requires_verified_platform_adapter",
            "task_read_resume": "ready" if configured else "requires_preview_root",
            "pdf_txt": "not_implemented_b03", "ocr": "not_implemented_b04",
            "speech_file": "not_implemented_b05", "refinement_playback": "not_implemented_b06",
            "microphone": "not_implemented_b07", "service_unload": "not_implemented_b08",
            "runtime_installer": "not_implemented_b09", "public_download": "not_implemented_b11"}


def diagnostics(configured=False):
    return {"preview": PREVIEW_ID, "product_version": PRODUCT_VERSION,
            "platform": sys.platform, "machine": platform.machine(),
            "os_release": platform.release(), "python": platform.python_version(),
            "worker_pid": os.getpid(), "isolated_python": bool(sys.flags.isolated),
            "bytecode_disabled": bool(sys.dont_write_bytecode),
            "transport": "anonymous-pipes-jsonl", "models_loaded": False,
            "production_tasks_opened": False, "gpu_verified": False,
            "shared_core": CORE_VERSION, "inference_on_open": False,
            "capabilities": capabilities(configured)}


class Worker:
    def __init__(self, session_id, output, task_root=None, boundary_helper=None, endpoint="http://127.0.0.1:11434"):
        self.session_id, self.output = session_id, output
        self.output_lock = threading.Lock()
        self.transfer_lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.sequence = 0
        self.hello = False
        self.seen = set()
        self.seen_order = deque()
        self.operation = None
        self.last_finished = None
        self.thread = None
        self.tasks = None
        self.endpoint = endpoint
        if task_root:
            self.tasks = TextTasks(TaskStore(task_root), MacBoundaries(boundary_helper or ""),
                                   engine_factory=lambda cancel: Ollama(self.endpoint, cancel=cancel))

    def emit(self, kind, req, **fields):
        with self.output_lock:
            self.sequence += 1
            self.output.write(encode(dict(protocol=PROTOCOL, session_id=self.session_id,
                request_id=req["request_id"], sequence=self.sequence, type=kind, **fields)))
            self.output.flush()

    def error(self, req, code, message=None):
        self.emit("error", req, error={"code": code, "message": message or code, "retryable": False})

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
            self.emit("ready", req, diagnostics=diagnostics(self.tasks is not None))
        elif not self.hello:
            self.error(req, "handshake_required")
        elif kind == "diagnose":
            self.emit("diagnostics", req, diagnostics=diagnostics(self.tasks is not None))
        elif kind.startswith("task.") or kind == "service.check":
            self.text_request(req)
        elif kind == "lease.renew":
            with self.operation_lock:
                op = self.operation
                if not op or (req.get("operation_id"), req.get("lease_id")) != (op["id"], op["lease"]):
                    self.error(req, "operation_or_lease_mismatch")
                elif op["cancel"].is_set():
                    self.error(req, "operation_cancelling")
                else:
                    op["deadline"] = time.monotonic() + op["lease_ms"] / 1000
                    self.emit("lease.renewed", req, operation_id=op["id"], lease_id=op["lease"])
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
                    if op.get("engine"): op["engine"].cancel()
                    if self.tasks: self.tasks.cancel(op["id"])
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
            op = {"id": op_id, "lease": lease_id, "cancel": threading.Event(), "lease_ms": lease_ms,
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
                if self.operation.get("engine"): self.operation["engine"].cancel()
                if self.tasks: self.tasks.cancel(self.operation["id"])
        if self.thread:
            self.thread.join()  # bounded probe: wait wakes immediately; no inference call

    def snapshot(self, req, snapshot, op=None):
        with self.transfer_lock:
            self._snapshot(req, snapshot, op)

    def _snapshot(self, req, snapshot, op=None):
        raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 32 * 1024 * 1024: raise CoreError("任务状态超出传输上限。")
        transfer = str(uuid.uuid4())
        fields = {} if op is None else dict(operation_id=op["id"], lease_id=op["lease"])
        self.emit("snapshot.begin", req, transfer_id=transfer, byte_count=len(raw), sha256=sha(raw), **fields)
        for index, offset in enumerate(range(0, len(raw), SNAPSHOT_CHUNK_BYTES)):
            self.emit("snapshot.chunk", req, transfer_id=transfer, index=index,
                      data=base64.b64encode(raw[offset:offset + SNAPSHOT_CHUNK_BYTES]).decode(), **fields)
        self.emit("snapshot.end", req, transfer_id=transfer, **fields)

    def text_request(self, req):
        if not self.tasks:
            self.error(req, "preview_root_required", "先指定独立预览任务目录。"); return
        try:
            kind = req["type"]
            if kind in ("task.start", "service.check"):
                self.start_text(req); return
            if self.operation and kind not in ("task.list", "task.export"):
                raise CoreError("请先安全取消当前操作。", "busy")
            if kind == "task.list":
                items, errors = self.tasks.store.list()
                # A task list can also exceed a JSONL frame; use the same bounded transfer.
                self.snapshot(req, {"listing": items, "errors": errors})
                self.emit("task.done", req)
            elif kind == "task.create":
                source = self.tasks.store.incoming(req.get("input_file"), req.get("sha256"), req.get("byte_count"))
                self.snapshot(req, self.tasks.create(source, req.get("direction")))
                self.emit("task.done", req)
            elif kind == "task.open":
                self.snapshot(req, self.tasks.open(req.get("task_id"))); self.emit("task.done", req)
            elif kind == "task.import":
                self.snapshot(req, self.tasks.import_copy(req.get("source_directory")))
                self.emit("task.done", req)
            elif kind == "task.save":
                self.snapshot(req, self.tasks.save()); self.emit("task.done", req)
            elif kind == "task.export":
                raw = self.tasks.export().encode()
                name = uuid.uuid4().hex + ".txt"
                atomic_write(raw, self.tasks.store.root / "exports" / name)
                self.emit("task.exported", req, filename=name, byte_count=len(raw), sha256=sha(raw))
            else: self.error(req, "unsupported_operation")
        except (CoreError, OSError, TypeError, ValueError) as exc:
            self.error(req, getattr(exc, "code", "task_io_error"), str(exc))

    def start_text(self, req):
        op_id, lease_id = identifier(req.get("operation_id")), identifier(req.get("lease_id"))
        lease_ms = req.get("lease_ms", 20000)
        if type(lease_ms) is not int or not 100 <= lease_ms <= 30000:
            raise CoreError("操作租约超出有界范围。")
        with self.operation_lock:
            if self.operation is not None: raise CoreError("已有活动操作。", "busy")
            op = dict(id=op_id, lease=lease_id, cancel=threading.Event(), lease_ms=lease_ms,
                      deadline=time.monotonic() + lease_ms / 1000, expired=False)
            self.operation = op
            self.emit("operation.started", req, operation_id=op_id, lease_id=lease_id,
                      lease_ms=lease_ms, result_kind="local-text" if req["type"] == "task.start" else "service-diagnostic")
            self.thread = threading.Thread(target=self.run_text, args=(req, op), daemon=False)
            self.thread.start()

    def run_text(self, req, op):
        ended = threading.Event()
        engine = None
        def watch_lease():
            while not ended.wait(.05):
                if time.monotonic() >= op["deadline"]:
                    op["expired"] = True; op["cancel"].set()
                    if engine: engine.cancel()
                    self.tasks.cancel(op["id"])
                    return
        watcher = threading.Thread(target=watch_lease, daemon=True); watcher.start()
        status, detail = "failed", None
        try:
            if req["type"] == "service.check":
                engine = Ollama(self.endpoint, cancel=op["cancel"])
                op["engine"] = engine
                detail = engine.verify(default_recipe(req.get("direction", "en-zh")))
                status = "completed"
            else:
                status = self.tasks.run(op["id"], op["cancel"], lambda snapshot: self.snapshot(req, snapshot, op))
        except Exception as exc:
            from translator_core import Cancelled
            status = "cancelled" if isinstance(exc, Cancelled) else "failed"
            detail = {"error": str(exc), "code": getattr(exc, "code", type(exc).__name__)}
        finally:
            ended.set(); watcher.join(timeout=1)
            if req["type"] == "task.start": self.snapshot(req, self.tasks.snapshot(), op)
            with self.operation_lock:
                self.emit("operation.finished", req, operation_id=op["id"], lease_id=op["lease"],
                          status="lease_expired" if op["expired"] else status, tail_drained=True,
                          result={"kind": "text-task" if req["type"] == "task.start" else "service-diagnostic", "detail": detail})
                self.last_finished = (op["id"], op["lease"]); self.operation = None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task-root")
    parser.add_argument("--boundary-helper")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    args = parser.parse_args(argv)
    worker = Worker(identifier(args.session_id), sys.stdout.buffer, args.task_root, args.boundary_helper, args.endpoint)
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
        if worker.tasks: worker.tasks.store.close()
