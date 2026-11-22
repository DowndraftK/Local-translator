"""Ephemeral input/correction and submit-intent state for any future UI."""
from .contracts import DictionaryQuery
from .rules import MODES, analyze_query


class DictionarySession:
    def __init__(self, raw="", mode="auto", revision=0):
        self.raw, self.mode, self.revision = raw, mode, revision
        self.worker_session = None
        self.resources = None
        self.expected = None
        self.result = None

    @property
    def analysis(self):
        return analyze_query(self.raw, self.mode)

    def invalidate(self):
        previous = self.expected["query_id"] if self.expected else None
        self.expected = self.result = None
        return previous

    def edit(self, raw):
        if raw == self.raw:
            return None  # Duplicate text notification preserves manual mode.
        old = self.invalidate()
        self.raw, self.mode, self.revision = raw, "auto", self.revision + 1
        return old

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("unsupported_direction")
        if mode == self.mode:
            return None
        old = self.invalidate(); self.mode = mode
        return old

    def prefill(self, raw):
        """Explicit selection transfer, including same text: a new auto revision.

        Ordinary edit retains its frozen same-text/manual semantics.
        """
        if not isinstance(raw, str):
            raise ValueError("invalid_query")
        old = self.invalidate()
        self.raw, self.mode, self.revision = raw, "auto", self.revision + 1
        return old

    def bind(self, worker_session, resources):
        if (worker_session, resources) == (self.worker_session, self.resources):
            return None
        old = self.invalidate()
        self.worker_session, self.resources = worker_session, resources
        return old

    def passive(self, event):
        if event not in ("page", "theme", "compositionend"):
            raise ValueError("unsupported_passive_event")
        # Retain input, correction and results. Returning no intent is deliberate.
        return None

    def submit(self, intent="button", *, composing=False, limit=20, cursors=None):
        if intent not in ("button", "enter") or composing or not self.analysis.directions:
            return None
        if self.worker_session is None or self.resources is None:
            return None
        query = DictionaryQuery.create(self.raw, self.mode, self.revision,
            self.worker_session, self.resources, limit=limit, cursors=cursors)
        self.expected, self.result = query.owner, None
        return query.wire()

    def accept(self, result):
        if not self.expected or result.get("owner") != self.expected:
            return False
        if result.get("status") == "cancelled":
            self.expected = None
        self.result = result
        return True
