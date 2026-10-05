"""B02 regressions against frozen production data and real loopback/subprocess boundaries."""
import base64
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(PROJECT / "shared"), str(PROJECT / "prototype/b01")]
from translator_core import Cancelled, CoreError
from translator_core.contracts import (bilingual_text, decode_snapshot, default_recipe, encode_snapshot,
                                       foundation_json, recipe_binding, sha, trim)
from translator_core.ollama import Ollama, endpoint
from translator_core.planning import MacBoundaries, QtBoundaries, split
from translator_core.store import TaskStore, atomic_write
from translator_core.tasks import TextTasks
from translator_preview.protocol import encode, request
from translator_preview.runtime import owned_environment, owned_python
from test_protocol import Peer

HELPER = PROJECT / "artifacts/b02-20261004/bin/text-boundaries"
OLD = PROJECT / "fixtures/b01/text-task/11111111-2222-4333-8444-555555555555"
SPLITS = PROJECT / "fixtures/b02/swift-splits.json"


def simple_boundaries(source, cancel):
    """Test-only ASCII oracle, never used by the native application."""
    offset = 0; rows = []
    for character in source:
        offset += len(character.encode())
        rows.append([offset, int(character in "\r\n"), int(character.isspace()), 0])
    return rows


def fake_result(source, recipe):
    return dict(source=source, translation="confirmed:" + source, model=recipe["model"], direction=recipe["direction"],
                elapsedSeconds=.125, warnings=[], configurationBinding=recipe_binding(recipe), requestConfiguration=deepcopy(recipe))


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="b02-test-"); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "tasks"
        self.store = TaskStore(self.root); self.addCleanup(self.store.close)
        self.core = TextTasks(self.store, simple_boundaries)

    def imported(self):
        return self.core.import_copy(OLD)["task"]

    def test_frozen_recipe_dialects_match_all_four_production_bytes(self):
        for version in (1, 2):
            for direction in ("en-zh", "zh-en"):
                raw = (PROJECT / f"fixtures/b01/swift-recipe-v{version}-{direction}.json").read_bytes()
                recipe = default_recipe(direction, version)
                self.assertEqual(foundation_json(recipe), raw)
                self.assertEqual(recipe_binding(recipe), sha(raw))
                if version == 1: self.assertNotIn("contextVersion", recipe)

    @unittest.skipUnless(sys.platform == "darwin", "Frozen NLTokenizer boundary evidence applies to Mac only")
    def test_all_frozen_native_splits_and_foundation_trim_are_equal(self):
        frozen = json.loads(SPLITS.read_bytes())
        for case in frozen["cases"]:
            with self.subTest(case=case["name"]):
                actual = split(case["source"], MacBoundaries(HELPER), threading.Event(), case["limit"])
                self.assertEqual(actual, case["segments"])
                self.assertEqual("".join(p["source"] for p in actual), case["source"])
        for source, expected in frozen["trims"].items(): self.assertEqual(trim(source), expected)

    def test_oversized_grapheme_fails_without_losing_saved_input(self):
        self.core.boundaries = MacBoundaries(HELPER) if sys.platform == "darwin" else QtBoundaries()
        source = "e" + "\u0301" * 1500
        self.core.create(source, "en-zh")
        self.assertEqual(self.core.run("generation", threading.Event(), lambda _: None), "failed")
        self.assertIn(source, self.core.export())
        self.assertEqual(self.core.task["text"]["segments"], [])

    def test_read_import_and_export_do_not_infer_or_write_source(self):
        before = {p.name: p.read_bytes() for p in OLD.iterdir()}
        task = self.imported(); original = deepcopy(task["text"]["segments"][0])
        def forbidden(**kwargs): self.fail("Opening created an inference engine")
        self.core.engine_factory = forbidden
        copied = {p.name: p.read_bytes() for p in self.store.directory(task["id"]).iterdir()}
        self.core.open(task["id"]); exported = self.core.export()
        self.assertIn("END-B01", exported); self.assertIn("自编夹具已保存结果。", exported)
        self.assertIn("尚未翻译", exported)
        self.assertEqual(original, self.core.task["text"]["segments"][0])
        self.assertEqual(before, {p.name: p.read_bytes() for p in OLD.iterdir()})
        self.assertEqual(copied, {p.name: p.read_bytes() for p in self.store.directory(task["id"]).iterdir()})

    def test_revision_high_water_rejects_late_write_after_reopen(self):
        task = self.imported(); task["revision"] = 3
        self.assertTrue(self.store.save(task))
        late = deepcopy(task); late["revision"] = 2
        self.assertFalse(self.store.save(late))
        self.store.close(); self.store = TaskStore(self.root); self.addCleanup(self.store.close)
        self.assertFalse(self.store.save(late)); self.assertEqual(self.store.read(task["id"])[0]["revision"], 3)

    def test_corrupt_current_reads_previous_and_blocks_continue_and_overwrite(self):
        task = self.imported(); current = self.store.directory(task["id"]) / "current.json"
        current.write_bytes(b"broken-current-evidence")
        snapshot = self.core.open(task["id"])
        self.assertEqual(snapshot["task"]["revision"], 1); self.assertTrue(snapshot["save_blocked"])
        with self.assertRaises(CoreError): self.core.run("g", threading.Event(), lambda _: None)
        with self.assertRaises(CoreError): self.core.save()
        self.assertEqual(current.read_bytes(), b"broken-current-evidence")

    def test_future_schema_never_falls_back_to_older_snapshot(self):
        task = self.imported(); current = self.store.directory(task["id"]) / "current.json"
        raw = b'{"schema":99}'; current.write_bytes(raw)
        with self.assertRaises(CoreError) as failure: self.core.open(task["id"])
        self.assertEqual(failure.exception.code, "future_schema"); self.assertEqual(current.read_bytes(), raw)

    def test_abnormal_reopen_is_memory_only_and_preserves_completed_prefix(self):
        task = self.imported(); success = deepcopy(task["text"]["segments"][0])
        task["revision"] = 3; task["text"]["phase"] = "running"; task["text"]["segments"][1]["state"] = "processing"
        self.store.save(task); current = self.store.directory(task["id"]) / "current.json"; raw = current.read_bytes()
        snapshot = self.core.open(task["id"])
        self.assertEqual(snapshot["task"]["text"]["phase"], "stopped")
        self.assertEqual(snapshot["task"]["text"]["segments"][1]["state"], "stopped")
        self.assertEqual(snapshot["task"]["text"]["segments"][0], success)
        self.assertEqual(current.read_bytes(), raw)

    def test_source_mismatch_is_readable_exportable_but_not_resumable(self):
        task = self.imported(); asset = self.store.directory(task["id"]) / task["asset"]["filename"]
        asset.write_bytes(b"mismatched")
        self.assertTrue(self.core.open(task["id"])["save_blocked"])
        self.assertIn("END-B01", self.core.export())
        with self.assertRaises(CoreError): self.core.run("g", threading.Event(), lambda _: None)
        self.assertEqual(asset.read_bytes(), b"mismatched")

    def test_asset_path_traversal_and_windows_aliases_are_rejected(self):
        task = self.imported(); task["revision"] = 3; task["asset"]["filename"] = "..\\escape.txt"
        with self.assertRaises(CoreError): self.store.save(task)
        for name in ("current.json ", "CURRENT.JSON", "source.txt.", "CON.txt", "source.txt:stream"):
            task["asset"]["filename"] = name
            with self.subTest(name=name), self.assertRaises(CoreError): self.store.save(task)

    def test_asset_symlink_is_rejected(self):
        self.imported()
        task = self.core.task; folder = self.store.directory(task["id"]); source = folder / task["asset"]["filename"]
        source.unlink()
        try: source.symlink_to(OLD / task["asset"]["filename"])
        except OSError:
            if sys.platform != "win32": raise
            self.skipTest("Windows user cannot create file symlinks; native junction protection checked separately")
        with self.assertRaises(CoreError): self.store.verify_asset(task, folder)

    @unittest.skipUnless(sys.platform == "win32", "Native NTFS junction check applies to Windows only")
    def test_windows_junction_cannot_import_or_replace_external_task(self):
        # The native PowerShell wrapper owns fixture creation and cleanup. Some
        # Windows policies deny Python reparse-point creation while allowing the
        # normal file tools; protection checks must still exercise a REAL junction.
        prepared = os.environ.get("B02_NATIVE_JUNCTION_FIXTURE")
        if not prepared:
            self.skipTest("Run Test-Windows-B02.ps1 to supply a real, owned NTFS junction fixture")
        junction = Path(prepared).absolute()
        self.assertTrue(junction.is_relative_to(PROJECT / "artifacts"))
        self.assertTrue(junction.is_junction())
        external = junction.resolve(strict=True)
        self.assertTrue(external.is_relative_to(PROJECT / "artifacts"))
        self.assertEqual(list(external.iterdir()), [])
        with self.assertRaises(CoreError): TaskStore(junction / "preview")
        with self.assertRaises(CoreError): self.store.import_copy(junction)
        self.assertEqual(list(external.iterdir()), [])

    def test_qt_unicode_packing_keeps_graphemes_complete_and_source_exact(self):
        boundaries = QtBoundaries()
        family = "👨‍👩‍👧‍👦"
        for source in ("e\u0301" * 40, family * 8, "\r\n" + "中文句子。下一句。\n" * 8):
            parts = split(source, boundaries, threading.Event(), 40)
            self.assertEqual("".join(p["source"] for p in parts), source)
            self.assertTrue(all(len(p["source"].encode()) <= 40 for p in parts))
            if source.startswith(family):
                self.assertTrue(all(p["source"] == family for p in parts))
            if source.startswith("e"):
                self.assertTrue(all(p["source"].endswith("\u0301") for p in parts))

    def test_new_adapter_identity_never_rewrites_legacy_plan_or_recipe(self):
        task = self.imported(); original = foundation_json(task["configuration"])
        class Boundaries:
            splitter_version = "qt6102-unicode-utf8-v1"
            def __call__(self, *_): raise AssertionError("Legacy plan must not be split again")
        self.core.boundaries = Boundaries()
        class Engine:
            def __init__(self, **kwargs): pass
            def translate(self, source, recipe): return fake_result(source, recipe)
        self.core.engine_factory = Engine
        self.assertEqual(self.core.run("g", threading.Event(), lambda _: None), "completed")
        self.assertEqual(foundation_json(self.core.task["configuration"]), original)
        fresh = TextTasks(self.store, QtBoundaries(), Engine)
        fresh.create("New Windows plan.\n", "en-zh")
        self.assertEqual(fresh.task["configuration"]["splitterVersion"], "qt6102-unicode-utf8-v1")
        self.assertEqual(fresh.run("fresh", threading.Event(), lambda _: None), "completed")
        self.core.create("Mac draft with no plan.", "en-zh")
        self.core.task["configuration"]["splitterVersion"] = "natural-language-utf8-v1"
        before = foundation_json(self.core.task)
        with self.assertRaises(CoreError) as failure: self.core.run("draft", threading.Event(), lambda _: None)
        self.assertEqual(failure.exception.code, "boundary_adapter_mismatch")
        self.assertEqual(before, foundation_json(self.core.task))

    def test_binding_mismatch_and_uuid_collision_do_not_replace_original(self):
        task = self.imported(); folder = self.store.directory(task["id"]); original = (folder / "current.json").read_bytes()
        task["revision"] = 3; task["configuration"]["options"]["temperature"] = .2
        with self.assertRaises(CoreError): self.store.save(task)
        with self.assertRaises(CoreError): self.core.import_copy(OLD)
        self.assertEqual((folder / "current.json").read_bytes(), original)

    def test_writer_lock_and_old_nonempty_root_are_rejected(self):
        with self.assertRaises(CoreError) as failure: TaskStore(self.root)
        self.assertEqual(failure.exception.code, "writer_busy")
        old = Path(self.temp.name) / "production"; old.mkdir(); (old / "valuable.txt").write_text("keep")
        with self.assertRaises(CoreError): TaskStore(old)
        self.assertEqual(list(old.iterdir()), [old / "valuable.txt"])

    def test_write_failure_retains_previous_snapshot(self):
        task = self.imported(); current = self.store.directory(task["id"]) / "current.json"; old = current.read_bytes()
        def fail(data, path):
            if path.name == "current.json": raise OSError("injected no space")
            atomic_write(data, path)
        self.store.write = fail; task["revision"] = 3
        with self.assertRaises(OSError): self.store.save(task)
        self.assertEqual(current.read_bytes(), old)
        self.assertEqual((current.parent / "previous.json").read_bytes(), old)

    def test_explicit_continue_skips_every_saved_success_without_replanning(self):
        task = self.imported(); old_part = deepcopy(task["text"]["segments"][0]); calls = []
        class Engine:
            def __init__(self, **kwargs): pass
            def translate(self, source, recipe): calls.append(source); return fake_result(source, recipe)
        self.core.engine_factory = Engine
        self.assertEqual(self.core.run("g", threading.Event(), lambda _: None), "completed")
        self.assertEqual(calls, [trim(task["text"]["segments"][1]["source"])])
        self.assertEqual(self.core.task["text"]["segments"][0], old_part)
        self.assertEqual(self.core.task["configuration"], task["configuration"])

    def test_cancel_discards_late_complete_response_and_can_explicitly_continue(self):
        self.imported(); entered = threading.Event(); release = threading.Event()
        class Engine:
            def __init__(self, **kwargs): pass
            def cancel(self): release.set()
            def translate(self, source, recipe): entered.set(); release.wait(2); return fake_result(source, recipe)
        self.core.engine_factory = Engine; result = []
        thread = threading.Thread(target=lambda: result.append(self.core.run("g", threading.Event(), lambda _: None)))
        thread.start(); self.assertTrue(entered.wait(2)); self.assertFalse(self.core.cancel("old-generation"))
        self.assertTrue(self.core.cancel("g")); thread.join(2); self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["cancelled"])
        part = self.core.task["text"]["segments"][1]
        self.assertEqual(part["state"], "stopped"); self.assertNotIn("result", part)
        self.assertEqual(self.core.run("new-g", threading.Event(), lambda _: None), "completed")

    def test_save_failure_stops_scheduling_but_keeps_confirmed_memory_export(self):
        self.core.create("first\n" + "second " * 400, "en-zh"); calls = []
        store = self.store
        class Engine:
            def __init__(self, **kwargs): pass
            def translate(self, source, recipe):
                calls.append(source)
                store.write = lambda *_: (_ for _ in ()).throw(OSError("injected disk full"))
                return fake_result(source, recipe)
        self.core.engine_factory = Engine
        self.assertEqual(self.core.run("g", threading.Event(), lambda _: None), "failed")
        self.assertEqual(len(calls), 1); self.assertTrue(self.core.save_blocked)
        self.assertIn("confirmed:first", self.core.export())
        saved = self.store.read(self.core.task["id"])[0]
        self.assertFalse(any(p["state"] == "completed" for p in saved["text"]["segments"]))

    def test_finite_attempts_require_user_runs_and_continue_other_segments(self):
        self.core.create("first\n" + "second " * 400, "en-zh"); calls = []
        class Engine:
            def __init__(self, **kwargs): pass
            def translate(self, source, recipe):
                calls.append(source)
                if source == "first": raise CoreError("truncated", "incomplete", False)
                return fake_result(source, recipe)
        self.core.engine_factory = Engine
        for generation in range(4): self.core.run(str(generation), threading.Event(), lambda _: None)
        self.assertEqual(calls.count("first"), 3)
        self.assertEqual(self.core.task["text"]["phase"], "partial")
        self.assertTrue(all(p["state"] == "completed" for p in self.core.task["text"]["segments"][1:]))

    def test_resource_failure_immediately_stops_later_segments(self):
        self.core.create("first\n" + "second " * 400, "en-zh"); calls = []
        class Engine:
            def __init__(self, **kwargs): pass
            def translate(self, source, recipe): calls.append(source); raise CoreError("wrong digest", "model_mismatch")
        self.core.engine_factory = Engine
        self.assertEqual(self.core.run("g", threading.Event(), lambda _: None), "failed")
        self.assertEqual(len(calls), 1); self.assertEqual(self.core.task["text"]["segments"][1]["state"], "pending")


class LocalAPI:
    def __init__(self):
        self.calls = []; self.mode = "normal"; self.entered = threading.Event(); self.release = threading.Event()
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, *args): pass
            def do_GET(self): self.reply()
            def do_POST(self): self.reply()
            def reply(self):
                count = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(count)) if count else None
                fixture.calls.append((self.path, body))
                recipe = default_recipe("en-zh")
                if self.path == "/api/version": value = {"version": "0.35.0"}
                elif self.path == "/api/tags":
                    model = {"name": recipe["model"], "digest": "0" * 64 if fixture.mode == "digest" else recipe["modelDigest"]}
                    if fixture.mode == "cloud": model["remote_host"] = "cloud.example"
                    value = {"models": [model]}
                elif self.path == "/api/show": value = {"capabilities": ["completion"]}
                else:
                    if fixture.mode != "body": fixture.entered.set()
                    if fixture.mode == "headers": fixture.release.wait(3)
                    chunks = [dict(message={"content": "已确认译文。"}, done=False)]
                    if fixture.mode != "truncated":
                        chunks += [dict(done=True, done_reason="length" if fixture.mode == "length" else "stop", eval_count=12)]
                    value = b"".join(json.dumps(c, ensure_ascii=False).encode() + b"\n" for c in chunks)
                if fixture.mode == "redirect":
                    self.send_response(302); self.send_header("Location", "http://example.invalid/"); self.end_headers(); return
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                if self.path == "/api/chat" and fixture.mode == "body":
                    fixture.entered.set(); fixture.release.wait(3)
                if self.path == "/api/chat" and fixture.mode == "slow_body":
                    fixture.release.wait(.35)
                try: self.wfile.write(raw)
                except (OSError, BrokenPipeError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.address = f"http://127.0.0.1:{self.server.server_port}"
    def close(self):
        self.release.set(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)


class OllamaCase(unittest.TestCase):
    def setUp(self):
        self.api = LocalAPI(); self.addCleanup(self.api.close); self.recipe = default_recipe("en-zh")
    def test_real_loopback_stream_uses_frozen_options_and_only_confirmed_completion(self):
        result = Ollama(self.api.address).translate("12 local notes.", self.recipe)
        self.assertEqual(result["translation"], "已确认译文。")
        body = self.api.calls[-1][1]
        self.assertEqual(body["options"], self.recipe["options"]); self.assertEqual(body["keep_alive"], "5m")
        self.assertNotIn("think", body); self.assertEqual(result["configurationBinding"], recipe_binding(self.recipe))
        self.assertEqual([p for p, _ in self.api.calls], ["/api/version", "/api/tags", "/api/show", "/api/chat"])
    def test_missing_done_and_length_done_never_become_success(self):
        for mode in ("truncated", "length"):
            self.api.mode = mode
            with self.assertRaises(CoreError) as failure: Ollama(self.api.address).translate("source", self.recipe)
            self.assertFalse(failure.exception.stops_scheduling)
    def test_wrong_digest_and_cloud_resources_rejected_before_chat(self):
        for mode in ("digest", "cloud"):
            self.api.mode = mode; self.api.calls.clear()
            with self.assertRaises(CoreError): Ollama(self.api.address).translate("source", self.recipe)
            self.assertNotIn("/api/chat", [p for p, _ in self.api.calls])
    def test_redirect_and_nonloopback_endpoints_rejected(self):
        self.api.mode = "redirect"
        with self.assertRaises(CoreError): Ollama(self.api.address).verify(self.recipe)
        self.assertEqual(len(self.api.calls), 1)
        for address in ("https://127.0.0.1", "http://localhost:11434", "http://127.0.0.1@remote", "http://127.0.0.1/x", "http://127.0.0.1?x=1"):
            with self.assertRaises(CoreError): endpoint(address)
    def test_cancellation_interrupts_headers_without_stopping_shared_server(self):
        self.api.mode = "headers"; engine = Ollama(self.api.address); errors = []
        def run():
            try: engine.translate("source", self.recipe)
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=run); thread.start(); self.assertTrue(self.api.entered.wait(2))
        start = time.monotonic(); engine.cancel(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertLess(time.monotonic() - start, 2)
        self.assertIsInstance(errors[0], Cancelled)
        self.assertEqual(Ollama(self.api.address).json("/api/version")["version"], "0.35.0")

    def test_cancellation_interrupts_buffered_body_without_stopping_shared_server(self):
        self.api.mode = "body"; engine = Ollama(self.api.address); errors = []
        def run():
            try: engine.translate("source", self.recipe)
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=run); thread.start(); self.assertTrue(self.api.entered.wait(2))
        start = time.monotonic(); engine.cancel(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertLess(time.monotonic() - start, 2)
        self.assertIsInstance(errors[0], Cancelled)
        self.assertEqual(Ollama(self.api.address).json("/api/version")["version"], "0.35.0")

    def test_polling_waits_do_not_truncate_a_slow_buffered_response(self):
        self.api.mode = "slow_body"
        result = Ollama(self.api.address).translate("source", self.recipe)
        self.assertEqual(result["translation"], "已确认译文。")
        self.assertGreater(result["elapsedSeconds"], .3)

    def test_stream_connection_abort_retains_cancel_or_service_failure_identity(self):
        # Reproduce Windows' post-header ConnectionAbortedError on both OSes.
        # The same read error without an owned cancel is never a success/cancel.
        from unittest.mock import patch
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                engine = Ollama(self.api.address)
                class Response:
                    def readline(self, _):
                        if cancel: engine.cancelled.set()
                        raise ConnectionAbortedError(10053, "Owned stream aborted")
                def request_stream(*_, **__): yield Response()
                with patch.object(engine, "verify", return_value={"capabilities": ["completion"]}), \
                     patch.object(engine, "_request", side_effect=request_stream):
                    with self.assertRaises(Cancelled if cancel else CoreError) as failure:
                        engine.translate("source", self.recipe)
                    if not cancel: self.assertEqual(failure.exception.code, "service_unavailable")


class WorkerTextCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="b02-worker-"); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "tasks"; self.session = str(uuid.uuid4()); self.events = queue.Queue()
        self.api = LocalAPI(); self.addCleanup(self.api.close)
        self.process = subprocess.Popen([owned_python(), "-I", "-B", "-X", "utf8", str(PROJECT / "prototype/b01/launch.py"), "worker",
            "--session-id", self.session, "--task-root", str(self.root), "--boundary-helper", str(HELPER),
            "--endpoint", self.api.address],
            env=owned_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.reader = threading.Thread(target=lambda: [self.events.put(json.loads(raw)) for raw in self.process.stdout], daemon=True)
        self.reader.start(); self.addCleanup(self.close)
        self.send("hello"); self.next("ready")
    def send(self, kind, **fields):
        req = request(kind, self.session, **fields); self.process.stdin.write(encode(req)); self.process.stdin.flush(); return req
    def next(self, kind=None):
        end = time.monotonic() + 5
        while True:
            event = self.events.get(timeout=max(.01, end - time.monotonic()))
            if kind is None or event["type"] == kind: return event
    def close(self):
        if self.process.poll() is None:
            self.send("shutdown"); self.process.wait(timeout=5)
        self.reader.join(2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr): stream.close()
    def snapshot(self):
        begin = self.next("snapshot.begin"); raw = bytearray(); index = 0
        while True:
            event = self.next()
            if event["type"] == "snapshot.end": break
            self.assertEqual(event["type"], "snapshot.chunk"); self.assertEqual(event["index"], index)
            self.assertEqual(event["transfer_id"], begin["transfer_id"])
            raw.extend(base64.b64decode(event["data"])); index += 1
        self.assertEqual(len(raw), begin["byte_count"]); self.assertEqual(sha(raw), begin["sha256"])
        return json.loads(raw)
    def test_large_input_and_snapshot_cross_frames_then_reopen_without_inference(self):
        source = "Saved local draft.\n" * 7000; raw = source.encode(); name = uuid.uuid4().hex + ".txt"
        (self.root / "incoming" / name).write_bytes(raw)
        self.send("task.create", input_file=name, byte_count=len(raw), sha256=sha(raw), direction="en-zh")
        snapshot = self.snapshot(); self.next("task.done"); task = snapshot["task"]
        self.assertEqual(task["input"], source); self.assertNotIn("text", task)
        current = self.root / task["id"] / "current.json"; before = current.read_bytes()
        self.send("task.open", task_id=task["id"])
        reopened = self.snapshot(); self.next("task.done"); self.assertEqual(reopened["task"], task)
        self.assertEqual(before, current.read_bytes())
        self.send("shutdown"); self.assertTrue(self.next("shutdown.confirmed")["tail_drained"])
        self.assertEqual(self.process.wait(timeout=5), 0)
    def test_invalid_incoming_identity_is_rejected_without_task_or_inference(self):
        self.send("task.create", input_file="../source.txt", byte_count=10, sha256="0" * 64, direction="en-zh")
        self.assertEqual(self.next("error")["error"]["code"], "unsafe_path")
        self.send("task.list"); self.assertEqual(self.snapshot()["listing"], []); self.next("task.done")
    def test_text_lease_expires_and_persists_before_terminal(self):
        raw = b"Local test source."; name = uuid.uuid4().hex + ".txt"
        (self.root / "incoming" / name).write_bytes(raw)
        self.send("task.create", input_file=name, byte_count=len(raw), sha256=sha(raw), direction="en-zh")
        task = self.snapshot()["task"]; self.next("task.done")
        self.api.mode = "headers"
        self.send("task.start", operation_id=str(uuid.uuid4()), lease_id=str(uuid.uuid4()), lease_ms=200)
        self.next("operation.started")
        self.assertTrue(self.api.entered.wait(2))
        terminal = self.next("operation.finished")
        self.assertEqual(terminal["status"], "lease_expired")
        persisted = decode_snapshot((self.root / task["id"] / "current.json").read_bytes())
        self.assertEqual(persisted["text"]["phase"], "stopped")
        self.assertEqual(persisted["text"]["segments"][0]["state"], "stopped")
        self.send("shutdown")
        confirmed = self.next("shutdown.confirmed")
        self.assertTrue(terminal["tail_drained"] and confirmed["tail_drained"])
        self.assertEqual(self.process.wait(timeout=5), 0)


class QtTextCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def wait(self, condition, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition(): return
            time.sleep(.005)
        self.fail("Qt text client timed out")

    def test_qt_large_snapshot_roundtrip_and_request_cleanup(self):
        from translator_preview.client import WorkerClient
        with tempfile.TemporaryDirectory(prefix="b02-qt-") as temp:
            root = Path(temp) / "tasks"
            client = WorkerClient(task_root=root, boundary_helper=HELPER)
            snapshots = []; client.snapshot.connect(snapshots.append)
            client.connect_worker(); self.wait(lambda: client.state == "ready")
            raw = ("Local draft.\n" * 8000).encode(); name = uuid.uuid4().hex + ".txt"
            (root / "incoming" / name).write_bytes(raw)
            client.send("task.create", input_file=name, byte_count=len(raw), sha256=sha(raw), direction="en-zh")
            self.wait(lambda: len(snapshots) == 1 and not client.requests)
            self.assertEqual(snapshots[0]["task"]["input"].encode(), raw)
            # Completed response identities must not leak through repeated queries.
            for _ in range(3):
                for index in range(200): client.send("diagnose")
                self.wait(lambda: not client.requests)
            self.assertIsNone(client.failure)
            client.shutdown(); self.wait(lambda: client.state == "disconnected")

    def test_qt_corrupt_or_reordered_transfer_is_rejected(self):
        from translator_preview.client import WorkerClient
        from translator_preview.protocol import ProtocolError
        client = WorkerClient()
        begin = dict(type="snapshot.begin", request_id="r", transfer_id="t", byte_count=2, sha256=sha(b"{}"))
        client.receive_snapshot(begin)
        with self.assertRaises(ProtocolError):
            client.receive_snapshot(dict(type="snapshot.chunk", request_id="r", transfer_id="t", index=1, data="e30="))
        client.receive_snapshot(dict(type="snapshot.chunk", request_id="r", transfer_id="t", index=0, data="e10="))
        with self.assertRaises(ProtocolError):
            client.receive_snapshot(dict(type="snapshot.end", request_id="r", transfer_id="t"))

    def test_qt_heartbeat_keeps_owned_long_request_alive_then_cancels(self):
        from translator_preview.client import WorkerClient
        with tempfile.TemporaryDirectory(prefix="b02-heartbeat-") as temp:
            api = LocalAPI(); self.addCleanup(api.close)
            root = Path(temp) / "tasks"
            client = WorkerClient(task_root=root, boundary_helper=HELPER, endpoint=api.address)
            client.connect_worker(); self.wait(lambda: client.state == "ready")
            raw = b"Owned request."; name = uuid.uuid4().hex + ".txt"
            (root / "incoming" / name).write_bytes(raw)
            snapshots = []; events = []; client.snapshot.connect(snapshots.append); client.event.connect(events.append)
            client.send("task.create", input_file=name, byte_count=len(raw), sha256=sha(raw), direction="en-zh")
            self.wait(lambda: snapshots and not client.requests)
            api.mode = "headers"
            self.assertTrue(client.start_operation("task.start"))
            self.wait(api.entered.is_set)
            client.heartbeat.setInterval(100)
            self.wait(lambda: sum(e["type"] == "lease.renewed" for e in events) >= 3)
            self.assertTrue(client.cancel()); self.wait(lambda: client.terminal is not None)
            self.assertEqual(client.terminal["status"], "cancelled")
            self.assertEqual(snapshots[-1]["task"]["text"]["segments"][0]["state"], "stopped")
            self.assertFalse(client.requests)
            client.shutdown(); self.wait(lambda: client.state == "disconnected")
            self.assertIsNone(client.failure)


if __name__ == "__main__": unittest.main(verbosity=2)
