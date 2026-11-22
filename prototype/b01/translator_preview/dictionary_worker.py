"""One dictionary executor and one replaceable pending request per worker."""
import threading
import time
from collections import deque

from translator_core.dictionary.contracts import DictionaryQuery, QUERY_VERSION
from translator_core.dictionary.query import DictionaryService
from translator_core.dictionary.resources import ResourceCatalog


class QueryCancel:
    def __init__(self):
        self.event = threading.Event()
        self.deadline = time.monotonic() + 5
    def set(self):
        self.event.set()
    def is_set(self):
        return self.event.is_set() or time.monotonic() >= self.deadline


class DictionaryWorker:
    def __init__(self, session, emit, config_path=None, catalog=None):
        self.session, self.emit = session, emit
        self.catalog = catalog if catalog is not None else ResourceCatalog(config_path)
        self.condition = threading.Condition()
        self.active = self.pending = None
        self.closed = False
        self.recent = deque(maxlen=128)
        self.stats = dict(max_active=0, max_pending=0, completed=0, replaced=0)
        self.thread = threading.Thread(target=self.run, name="dictionary-query", daemon=False)
        self.thread.start()

    def description(self):
        return dict(contract=QUERY_VERSION, resources=self.catalog.snapshot(),
                    max_active=1, max_pending=1, query_deadline_seconds=5, terminal_history_limit=128,
                    scheduler_stats=dict(self.stats))

    def failure(self, req, code):
        self.emit("dictionary.error", req, error=dict(code=code, message=code, retryable=False))

    def handle(self, req):
        kind = req["type"]
        if kind == "dictionary.resources":
            memory = {"available": False}
            try:
                import resource
                import sys
                value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                memory = dict(available=True, peak_rss_bytes=value if sys.platform == 'darwin' else value * 1024,
                              method="worker-self-getrusage", scope="whole_worker_process")
            except ImportError:
                pass
            self.emit("dictionary.resources", req, process_memory=memory, **self.description())
        elif kind == "dictionary.query":
            try:
                query = DictionaryQuery.parse(req.get("query"), self.session)
            except (ValueError, TypeError, UnicodeError) as exc:
                self.failure(req, str(exc)); return
            job = dict(req=req, query=query, cancel=QueryCancel())
            with self.condition:
                query_id = query.owner["query_id"]
                if self.closed:
                    self.failure(req, "dictionary_closed"); return
                if query_id in self.recent or any(j and j["query"].owner["query_id"] == query_id for j in (self.active, self.pending)):
                    self.failure(req, "duplicate_query_id"); return
                if self.active:
                    self.active["cancel"].set()
                if self.pending:
                    self.stats["replaced"] += 1
                    self.finish_cancelled(self.pending)
                self.pending = job
                self.stats["max_pending"] = max(self.stats["max_pending"], 1)
                self.condition.notify()
        elif kind == "dictionary.cancel":
            from translator_core.dictionary.contracts import identifier
            try:
                query_id = identifier(req.get("query_id"))
            except ValueError as exc:
                self.failure(req, str(exc)); return
            with self.condition:
                found = False
                if self.active and self.active["query"].owner["query_id"] == query_id:
                    self.active["cancel"].set(); found = True
                if self.pending and self.pending["query"].owner["query_id"] == query_id:
                    self.finish_cancelled(self.pending); self.pending = None; found = True
                self.emit("dictionary.cancelled", req, query_id=query_id,
                    status="cancel_requested" if found else "already_finished" if query_id in self.recent else "not_owned")
        else:
            self.failure(req, "unsupported_dictionary_operation")

    def finish_cancelled(self, job):
        query = job["query"]
        self.emit("dictionary.result", job["req"], result=dict(version=QUERY_VERSION,
            owner=query.owner, analysis=query.analysis, status="cancelled", groups=[]))
        self.recent.append(query.owner["query_id"])
        self.stats["completed"] += 1

    def run(self):
        service = DictionaryService(self.catalog, self.session)
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.closed or self.pending is not None)
                    if self.closed:
                        return
                    job, self.pending = self.pending, None
                    self.active = job; self.stats["max_active"] = 1
                try:
                    result = service.query(job["query"].wire(), job["cancel"])
                except Exception as exc:
                    result = dict(version=QUERY_VERSION, owner=job["query"].owner,
                                  analysis=job["query"].analysis, status="query_failed", groups=[],
                                  error_code=type(exc).__name__)
                with self.condition:
                    # Cancellation/replacement also wins in the tiny SQL-to-send
                    # window. Emission and ownership release are ordered here.
                    if job["cancel"].is_set():
                        self.finish_cancelled(job)
                    else:
                        self.emit("dictionary.result", job["req"], result=result)
                        self.recent.append(job["query"].owner["query_id"])
                        self.stats["completed"] += 1
                    self.active = None
        finally:
            service.close()

    def close(self):
        with self.condition:
            self.closed = True
            if self.active:
                self.active["cancel"].set()
            if self.pending:
                self.finish_cancelled(self.pending); self.pending = None
            self.condition.notify_all()
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            raise RuntimeError("dictionary_tail_not_drained")
