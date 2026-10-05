"""Single shared text workflow. UI and IPC never implement scheduling or recipes."""
import threading
import time
import uuid
from copy import deepcopy

from . import Cancelled, CoreError
from .contracts import (MAX_INPUT_BYTES, bilingual_text, default_recipe, foundation_json,
                        recover_in_memory, recipe_binding, sha, trim, validate_recipe, validate_task)
from .ollama import Ollama
from .planning import split

FOUNDATION_EPOCH = 978307200


class TextTasks:
    def __init__(self, store, boundaries, engine_factory=Ollama):
        self.store, self.boundaries, self.engine_factory = store, boundaries, engine_factory
        self.lock = threading.RLock()
        self.task = None
        self.warning = None
        self.durable_revision = None
        self.save_blocked = False
        self.active = None
        self.engine = None

    def snapshot(self):
        with self.lock:
            return {"task": deepcopy(self.task), "warning": self.warning,
                    "durable_revision": self.durable_revision, "save_blocked": self.save_blocked,
                    "active": self.active is not None}

    def idle(self):
        if self.active is not None: raise CoreError("请先安全取消当前操作，再切换任务。", "busy")

    def create(self, source, direction, name=None):
        with self.lock:
            self.idle()
            if not isinstance(source, str) or not trim(source) or len(source.encode()) > MAX_INPUT_BYTES:
                raise CoreError("请输入非空原文，预览上限1MiB UTF-8。")
            recipe = default_recipe(direction)
            recipe["splitterVersion"] = getattr(self.boundaries, "splitter_version", recipe["splitterVersion"])
            identity = str(uuid.uuid4()).upper(); now = time.time() - FOUNDATION_EPOCH
            raw = source.encode(); fingerprint = sha(raw)
            task = dict(id=identity, revision=1, created=now, updated=now, name=(name or trim(source)[:32]),
                        kind="文字", input=source, configuration=recipe, drafts={},
                        asset=dict(originalPath="b02://typed-input", filename=f"source-{fingerprint}.txt",
                                   byteCount=len(raw), fingerprint=fingerprint))
            self.store.save(task, raw)
            self.task, self.warning, self.durable_revision, self.save_blocked = task, None, 1, False
            return self.snapshot()

    def open(self, identity):
        with self.lock:
            self.idle()
            task, warning = self.store.read(identity)
            self.task = recover_in_memory(task)
            self.warning = warning; self.save_blocked = bool(warning)
            self.durable_revision = task["revision"]
            return self.snapshot()

    def import_copy(self, source):
        with self.lock:
            self.idle(); task, _ = self.store.import_copy(source)
            return self.open(task["id"])

    def _persist(self, changed):
        self.task["revision"] += 1
        self.task["updated"] = time.time() - FOUNDATION_EPOCH
        try:
            if not self.store.save(self.task):
                raise CoreError("拒绝迟到旧序号，未继续调度。", "stale_revision")
        except (OSError, CoreError) as exc:
            self.save_blocked = True
            self.warning = "保存失败，已停止继续调度；内存中确认结果仍可完整导出：" + str(exc)
            if self.task.get("text"):
                self.task["text"]["phase"] = "failed"
                self.task["text"]["error"] = self.warning
            changed(self.snapshot())
            raise CoreError(self.warning, "save_failed") from exc
        self.durable_revision = self.task["revision"]
        changed(self.snapshot())

    def save(self, changed=lambda _: None):
        with self.lock:
            self.idle()
            if self.task is None: raise CoreError("请先保存或打开任务。")
            if self.save_blocked:
                raise CoreError("此任务保存已受阻；请完整导出后另建任务。", "save_blocked")
            self._persist(changed)
            return self.snapshot()

    def cancel(self, generation):
        with self.lock:
            if not self.active or self.active[0] != generation: return False
            self.active[1].set()
            if self.engine: self.engine.cancel()
            return True

    def export(self):
        with self.lock:
            if self.task is None: raise CoreError("请先打开任务。")
            return bilingual_text(deepcopy(self.task))

    def _source_key(self):
        return self.task["input"]

    def _plan(self, cancel):
        return split(self.task["input"], self.boundaries, cancel)

    def _before_run(self):
        pass

    def run(self, generation, cancel, changed, started=lambda: None):
        with self.lock:
            self.idle()
            if self.task is None: raise CoreError("请先保存新任务或打开已有任务。")
            if self.save_blocked: raise CoreError("保存/源副本受阻，禁止继续；可完整导出后另建任务。", "save_blocked")
            validate_recipe(self.task["configuration"])
            if (not (self.task.get("text") or {}).get("segments") and
                    not (self.task.get("document") or {}).get("segments") and
                    self.task["configuration"]["splitterVersion"] != getattr(
                        self.boundaries, "splitter_version", "natural-language-utf8-v1")):
                raise CoreError("未分段任务需要原分句适配器；请在原平台继续或另建任务。", "boundary_adapter_mismatch")
            self.store.verify_asset(self.task, self.store.directory(self.task["id"]))
            self._before_run()
            self.active = (generation, cancel)
            self.engine = self.engine_factory(cancel=cancel)
            recipe = deepcopy(self.task["configuration"])
            if not self.task.get("text"):
                self.task["text"] = dict(id=self.task["id"], source=self._source_key(), model=recipe["model"],
                                         direction=recipe["direction"], phase="preparing", segments=[])
            job = self.task["text"]
            job.pop("error", None)
            started()
        try:
            if not job["segments"]:
                parts = self._plan(cancel)
                with self.lock:
                    self._current(generation); job["segments"] = parts
            with self.lock:
                self._current(generation); job["phase"] = "running"; self._persist(changed)
            for index in range(len(job["segments"])):
                with self.lock:
                    self._current(generation)
                    part = job["segments"][index]
                    attempts = job.setdefault("attempts", {})
                    key = str(index)
                    if part["state"] == "completed" or attempts.get(key, 0) >= 3: continue
                    attempts[key] = attempts.get(key, 0) + 1
                    part["state"] = "processing"; part.pop("error", None)
                    self._persist(changed)
                    source = trim(part["source"])
                try:
                    result = self.engine.translate(source, recipe)
                    with self.lock:
                        # Cancellation/replacement wins even if the owned socket
                        # returned success just before the control thread ran.
                        self._current(generation)
                        if (result.get("source") != source or result.get("model") != recipe["model"] or
                                result.get("direction") != recipe["direction"] or not trim(result.get("translation", "")) or
                                result.get("configurationBinding") != recipe_binding(recipe)):
                            raise CoreError("结果与本段配置身份不匹配。", "incomplete", False)
                        part["result"] = result; part["state"] = "completed"
                except CoreError as exc:
                    with self.lock:
                        self._current(generation); part["state"] = "failed"; part["error"] = str(exc)
                        if exc.stops_scheduling:
                            job["phase"] = "failed"; job["error"] = str(exc)
                            self._persist(changed); return "failed"
                with self.lock:
                    self._current(generation); self._persist(changed)
            with self.lock:
                self._current(generation)
                job["phase"] = "completed" if all(p["state"] == "completed" for p in job["segments"]) else "partial"
                self._persist(changed)
                return job["phase"]
        except Cancelled:
            with self.lock:
                job["phase"] = "stopped"
                for part in job["segments"]:
                    if part["state"] == "processing": part["state"] = "stopped"
                if not self.save_blocked: self._persist(changed)
                return "cancelled"
        except CoreError as exc:
            with self.lock:
                if not self.save_blocked:
                    job["phase"] = "failed"; job["error"] = str(exc)
                    for part in job["segments"]:
                        if part["state"] == "processing": part["state"] = "failed"; part["error"] = str(exc)
                    self._persist(changed)
            return "failed"
        finally:
            with self.lock:
                self.engine = None; self.active = None

    def _current(self, generation):
        if not self.active or self.active[0] != generation or self.active[1].is_set(): raise Cancelled()
