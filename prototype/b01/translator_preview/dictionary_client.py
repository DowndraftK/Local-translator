"""Dictionary-only Qt transport/state; never touches task/operation state."""
from PySide6.QtCore import QObject, Signal

from translator_core.dictionary.contracts import QUERY_VERSION, MAX_PAGE_BYTES, encoded
from translator_core.dictionary.session import DictionarySession


class DictionaryClient(QObject):
    result = Signal(dict)
    event = Signal(dict)
    changed = Signal(str)

    def __init__(self, client):
        super().__init__(client)
        self.client = client
        self.session = DictionarySession()
        self.available = False
        self.state = "disconnected"
        self.inflight = {}
        self.cancels = {}
        self.pending = None
        self.resource_request = None
        self.rejected_responses = 0
        self.max_inflight = 0

    def set_state(self, state):
        self.state = state; self.changed.emit(state)

    def disconnect(self):
        self.available = False
        self.pending = self.resource_request = None
        self.inflight.clear(); self.cancels.clear()
        self.session.bind(None, None)
        self.set_state("disconnected")

    def configure(self, diagnostics):
        description = diagnostics.get("dictionary")
        capability = diagnostics.get("capabilities", {}).get("dictionary")
        if capability != QUERY_VERSION or not isinstance(description, dict):
            self.available = False; self.session.invalidate()
            self.set_state("unsupported_capability"); return
        if description.get("contract") != QUERY_VERSION:
            self.available = False; self.session.invalidate()
            self.set_state("unsupported_contract"); return
        self.available = True
        previous = self.session.bind(self.client.session_id, description["resources"])
        self._cancel_owned(previous)
        self.set_state("idle")

    def _cancel_owned(self, query_id):
        if not query_id or query_id in self.cancels.values():
            return False
        if any(owner["query_id"] == query_id for owner in self.inflight.values()):
            req = self.client.send("dictionary.cancel", query_id=query_id)
            if req:
                self.cancels[req] = query_id; return True
        return False

    def edit(self, raw):
        if raw == self.session.raw:
            return False
        previous = self.session.edit(raw)
        self.pending = None; self._cancel_owned(previous)
        self.set_state(self.session.analysis.status)
        return True

    def set_mode(self, mode):
        if mode == self.session.mode:
            return False
        previous = self.session.set_mode(mode)
        self.pending = None; self._cancel_owned(previous)
        self.set_state(self.session.analysis.status)
        return True

    def prefill(self, raw):
        previous = self.session.prefill(raw)
        self.pending = None; self._cancel_owned(previous)
        self.set_state(self.session.analysis.status)

    def passive(self, event):
        return self.session.passive(event)

    def submit(self, intent="button", *, composing=False, limit=20, cursors=None):
        if not self.available or self.client.state not in ("ready", "running", "cancelling"):
            return None
        previous = self.session.expected
        query = self.session.submit(intent, composing=composing, limit=limit, cursors=cursors)
        if query is None:
            return None
        self._cancel_owned(previous["query_id"] if previous else None)
        self.pending = query  # Replace the one local unsent latest request.
        self.set_state("querying"); self._pump()
        return query["owner"]["query_id"]

    def _pump(self):
        if self.pending is None or len(self.inflight) >= 2 or self.client.state == "closing":
            return
        query, self.pending = self.pending, None
        req = self.client.send("dictionary.query", query=query)
        if req:
            self.inflight[req] = query["owner"]
            self.max_inflight = max(self.max_inflight, len(self.inflight))
        else:
            self.session.invalidate(); self.set_state("query_failed")

    def cancel(self, query_id=None):
        expected = self.session.expected
        query_id = query_id or (expected["query_id"] if expected else None)
        if not query_id:
            return False
        if self.pending and self.pending["owner"]["query_id"] == query_id:
            self.pending = None
        sent = self._cancel_owned(query_id)
        if expected and expected["query_id"] == query_id:
            self.session.invalidate(); self.set_state("cancelled")
        return sent

    def describe(self):
        if not self.available or self.resource_request or self.client.state not in ("ready", "running", "cancelling"):
            return None
        self.resource_request = self.client.send("dictionary.resources")
        return self.resource_request

    def close(self):
        self.pending = None
        self.session.invalidate()
        for owner in tuple(self.inflight.values()):
            self._cancel_owned(owner["query_id"])
        self.set_state("closing")

    def receive(self, event, request_kind):
        """Consume even stale terminals so request registration stays bounded.

        A dictionary rejection is local to this channel; it never changes the
        parent's busy/ready/failed state or clears a translation operation.
        """
        kind, req = event["type"], event["request_id"]
        if request_kind == "dictionary.query":
            owner = self.inflight.pop(req, None)
            if kind == "dictionary.result":
                result = event.get("result", {})
                if (owner is None or result.get("version") != QUERY_VERSION or result.get("owner") != owner
                        or len(encoded(result)) > MAX_PAGE_BYTES):
                    self.rejected_responses += 1
                elif self.session.expected != owner:
                    # Resource-change terminals are query-owned too. A late old
                    # query must not rebind resources or clear a newer result.
                    self.rejected_responses += 1
                elif result.get("status") == "resource_changed":
                    self.session.bind(self.client.session_id, result["resources"])
                    self.pending = None
                    self.set_state("resource_changed")
                elif self.session.accept(result):
                    self.set_state(result["status"]); self.result.emit(result)
                else:
                    self.rejected_responses += 1
            elif kind == "dictionary.error":
                if self.session.expected == owner:
                    self.session.invalidate(); self.set_state(event["error"]["code"])
            else:
                self.rejected_responses += 1
        elif request_kind == "dictionary.cancel":
            owner_id = self.cancels.pop(req, None)
            if kind != "dictionary.cancelled" or event.get("query_id") != owner_id:
                self.rejected_responses += 1
        elif request_kind == "dictionary.resources":
            self.resource_request = None
            if kind == "dictionary.resources" and event.get("contract") == QUERY_VERSION:
                previous = self.session.bind(self.client.session_id, event["resources"])
                if previous:
                    self.pending = None; self._cancel_owned(previous)
                    self.set_state("resource_changed")
            elif kind == "dictionary.error":
                self.set_state(event["error"]["code"])
            else:
                self.set_state("unsupported_contract")
        self.event.emit(event)
        self._pump()
