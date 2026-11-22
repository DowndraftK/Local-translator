"""Small page/status/summary contracts over WorkerClient, without a second scheduler.

Result contracts are presentation-only v1. They do not change the stored task
schema or introduce a detailed-generation request. T03 owns that integration.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from PySide6.QtCore import QObject, Signal

FOUNDATION_EPOCH = 978307200
PHASES = {"saved":"来源已保存", "decoding":"正在解码", "recognizing":"正在识别", "refining":"正在精修", "draft": "已保存原文", "preparing": "正在分段", "running": "正在处理",
          "completed": "已完成", "stopped": "已暂停", "partial": "部分失败", "failed": "失败"}


@dataclass(frozen=True)
class TaskSummary:
    task_id: str
    kind: str
    title: str
    updated: float
    created: float | None
    phase: str
    completed: int | None
    total: int | None
    direction: str | None
    revision: int | None
    source_ref: dict | None
    warning: str | None
    # Common sidecar metadata; body revision/time keep their original meaning.
    favorite: bool | None = False
    renamed_title: str | None = None
    trashed_at: float | None = None
    metadata_revision: int = 0

    @classmethod
    def from_listing(cls, row):
        return cls(row["id"], row["kind"], row["name"], row["updated"], row.get("created"),
                   row["phase"], row.get("completed"), row.get("total"), row.get("direction"),
                   row.get("revision"), row.get("source_ref"), row.get("warning"),
                   row.get("favorite",False),row.get("renamed_title"),row.get("trashed_at"),row.get("metadata_revision",0))

    @property
    def status(self):
        return PHASES.get(self.phase, self.phase)

    @property
    def time_label(self):
        # Saved timestamps use Foundation seconds. Render in the user's local OS zone.
        return datetime.fromtimestamp(self.updated + FOUNDATION_EPOCH).strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class TaskState:
    task_id: str | None = None
    phase: str = "empty"
    completed: int = 0
    total: int = 0
    error: str | None = None
    save_status: str = "unsubmitted"
    durable_revision: int | None = None
    can_start: bool = False
    can_cancel: bool = False
    can_save: bool = False
    can_export: bool = False
    operation_kind: str | None = None

    @classmethod
    def map(cls, snapshot, connection, operation_kind=None, operation_error=None, pending=False):
        task = (snapshot or {}).get("task")
        if not task:
            return cls(can_cancel=connection == "running", error=operation_error, operation_kind=operation_kind)
        job, doc = task.get("text") or {}, task.get("document") or task.get('image') or {}
        audio = task.get("audio")
        parts = audio["cues"] if audio else job.get("segments", [])
        durable = snapshot.get("durable_revision")
        blocked = bool(snapshot.get("save_blocked"))
        phase = audio["phase"] if audio else job.get("phase", doc.get("phase", "draft"))
        ready = connection == "ready" and not pending
        return cls(task["id"], phase, sum(p["state"] == "completed" for p in parts), len(parts),
                   snapshot.get("warning") or (audio or {}).get("error") or job.get("error") or doc.get("issue") or operation_error,
                   "blocked" if blocked else "saved" if durable == task["revision"] else "unsaved",
                   durable, ready and not blocked and not (doc and task.get("drafts")), connection == "running",
                   ready and not blocked, connection in ("ready", "running", "cancelling"), operation_kind)


class WorkspaceBridge(QObject):
    """One summary source for both views; task bodies remain worker-owned."""
    summaries_changed = Signal()
    state_changed = Signal()
    task_opened = Signal(str)
    location_ready = Signal(dict)

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.summaries = ()
        self.errors = ()
        self.snapshot = None
        self.operation_error = None
        self.refresh_request = None
        self.open_request = None
        self.open_identity = None
        self.client.snapshot.connect(self.receive_snapshot)
        self.client.changed.connect(self.connection_changed)
        self.client.event.connect(self.receive_event)
        if getattr(self.client,"history",None):
            self.client.history.result.connect(self.receive_history)
            self.client.history.changed.connect(self.history_state_changed)

    def history_state_changed(self,scope):
        if scope=='history' and self.client.history.views[scope]['state'] in ('failed','cancelled','unavailable','disconnected'):
            self.refresh_request=None

    def receive_history(self,scope,result):
        if scope=='history' and result['owner']['query_id']==self.refresh_request:
            self.refresh_request=None
        if scope=='history' and not any(self.client.history.views[scope]['filters'].get(k) for k in ('text','kind','status','favorite','trash')):
            self.summaries=tuple(TaskSummary.from_listing(row) for row in result['items'])
            self.errors=tuple(issue['message'] for issue in result['errors']);self.summaries_changed.emit()

    @property
    def state(self):
        return TaskState.map(self.snapshot, self.client.state,
                             self.client.operation_kind if self.client.operation else None, self.operation_error,
                             self.client.task_request_pending())

    def read_summaries(self, kind=None):
        return tuple(s for s in self.summaries if kind is None or s.kind == kind)

    def refresh(self):
        if getattr(self.client,'history',None) and self.client.history.available:
            self.client.history.refresh_all()
            owner=self.client.history.views.get('history',{}).get('owner')
            self.refresh_request=owner['query_id'] if owner else None
            return True
        if self.client.state not in ("ready", "running", "cancelling") or self.refresh_request:
            return False
        self.refresh_request = self.client.send("task.list")
        return bool(self.refresh_request)

    def open_task(self, task_id, locator=None):
        if self.client.state != "ready" or self.open_request or self.client.task_request_pending():
            return False
        fields = dict(task_id=task_id)
        if locator is not None:
            fields['locator'] = locator
        self.open_request = self.client.send("task.open", **fields)
        self.open_identity = task_id if self.open_request else None
        return bool(self.open_request)

    def connection_changed(self, state):
        if state == "connecting":
            self.snapshot = None
            self.operation_error = None
        if state in ("disconnected", "failed"):
            self.refresh_request = self.open_request = None
        self.state_changed.emit()

    def receive_snapshot(self, snapshot):
        if "listing" in snapshot:
            self.summaries = tuple(TaskSummary.from_listing(row) for row in snapshot["listing"])
            self.errors = tuple(snapshot.get("errors", ()))
            self.summaries_changed.emit()
        elif "task" in snapshot:
            if (snapshot.get("task") or {}).get("id") != ((self.snapshot or {}).get("task") or {}).get("id"):
                self.operation_error = None
            self.snapshot = snapshot
            self.state_changed.emit()
            context = self.client.snapshot_context or {}
            if (self.open_request and context.get('request') == self.open_request and
                    context.get('session') == self.client.session_id and
                    (snapshot.get('task') or {}).get('id') == self.open_identity):
                self.task_opened.emit(snapshot["task"]["kind"])

    def receive_event(self, event):
        kind = event["type"]
        if (kind.startswith('resource.') or kind=='operation.started' and event.get('result_kind')=='resource.check' or
                kind=='operation.finished' and event.get('result',{}).get('kind')=='resource-check'):
            self.state_changed.emit(); return
        if kind == "ready":
            self.refresh()
        elif kind == "operation.started":
            self.operation_error = None
        elif kind == "error":
            self.operation_error = event["error"].get("message", event["error"]["code"])
        elif kind == "operation.finished":
            detail = event.get("result", {}).get("detail")
            self.operation_error = detail.get("error") if isinstance(detail, dict) else None
            self.refresh()
        if kind in ("task.done", "error"):
            if event["request_id"] == self.refresh_request:
                self.refresh_request = None
            elif event["request_id"] == self.open_request:
                if kind == 'task.done' and self.snapshot and self.snapshot.get('history_location'):
                    self.location_ready.emit(self.snapshot['history_location'])
                self.open_request = None
                self.open_identity = None
            elif kind == "task.done":
                self.refresh()
        self.state_changed.emit()


class PageLifecycle:
    """Persistent widgets keep drafts on navigation. Opening tasks is explicit."""
    def enter_page(self):
        self.page_active = True

    def leave_page(self):
        self.page_active = False

    def capture_draft(self):
        return None

    def restore_draft(self, draft):
        pass


@dataclass(frozen=True)
class ResultOwner:
    input_sha256: str
    direction: str
    configuration_sha256: str
    effective_revision: str
    task_id: str | None = None
    task_revision: int | None = None
    request_id: str | None = None
    generation: str | None = None

    @classmethod
    def for_input(cls, text, direction, configuration, effective_revision="draft", **identity):
        digest = lambda raw: hashlib.sha256(raw).hexdigest()
        return cls(digest(text.encode()), direction,
                   digest(json.dumps(configuration, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()),
                   effective_revision, **identity)


@dataclass(frozen=True)
class Alternative:
    text: str
    context: str
    explanation: str


@dataclass(frozen=True)
class Example:
    source: str
    translation: str


@dataclass(frozen=True)
class Explanation:
    alternatives: tuple[Alternative, ...] = ()
    usage_notes: tuple[str, ...] = ()
    examples: tuple[Example, ...] = ()


@dataclass(frozen=True)
class TranslationResult:
    owner: ResultOwner
    translation: str
    explanation: Explanation | None = None
    version: int = 1

    def __post_init__(self):
        if self.version != 1 or not isinstance(self.translation, str) or not self.translation.strip():
            raise ValueError("不支持的展示结果版本或空主译文")
        if self.explanation is not None and not isinstance(self.explanation, Explanation):
            raise ValueError("详解必须以独立字段传入，不能将响应JSON充当主译文")


class ResultPresentation:
    """R7 invalidation; no model request, storage write, or inferred detail."""
    def __init__(self):
        self.owner = None
        self.result = None
        self.mode = "concise"

    def bind(self, owner):
        if owner != self.owner:
            self.result = None
            self.owner = owner

    def accept(self, result):
        if result.owner != self.owner or (self.mode == "detailed" and result.explanation is None):
            return False
        self.result = result
        return True

    def set_mode(self, mode):
        if mode not in ("concise", "detailed"):
            raise ValueError("未知文字展示模式")
        if mode == "detailed" and self.result and self.result.explanation is None:
            self.result = None
        self.mode = mode

    def clear(self):
        self.result = None
