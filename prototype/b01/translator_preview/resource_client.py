"""Resource ownership over the existing worker operation and shutdown path."""
from copy import deepcopy
from PySide6.QtCore import QObject, Signal
from translator_core.resources import CONTRACT, validate_description


class ResourceClient(QObject):
    changed = Signal()

    def __init__(self, client):
        super().__init__(client)
        self.client = client
        self.description = None; self.expected = None; self.pending = None
        self.last_description = None
        self.error = None; self.progress = None; self.rejected = 0
        self.guard = lambda: True

    def disconnect(self):
        self.description = self.expected = self.pending = self.progress = None
        self.changed.emit()

    def configure(self, diagnostics):
        if diagnostics.get('capabilities', {}).get('resources') != CONTRACT:
            self.error = dict(message='当前工作进程不支持本地资源接口。')
        else:
            self.accept(diagnostics.get('resources'))

    def accept(self, value):
        validate_description(value)
        self.description = deepcopy(value)
        self.last_description = deepcopy(value)
        self.error = value.get('warning'); self.changed.emit()

    def action(self, kind, **fields):
        if self.pending or self.client.state != 'ready' or self.client.task_request_pending(): return False
        if kind in ('resource.select', 'resource.check') and not self.guard(): return False
        if self.description:
            fields.setdefault('selection_revision',self.description['selection_revision'])
            fields.setdefault('resource_set_id',self.description['resource_set_id'])
        self.pending = self.client.send(kind, **fields)
        self.error = None; self.changed.emit()
        return self.pending

    def describe(self):
        if self.pending or self.client.state not in ('ready','running','cancelling'):return False
        self.pending=self.client.send('resource.describe')
        return self.pending

    def check(self, runtime=False):
        if self.pending or self.client.state != 'ready' or self.client.task_request_pending() or not self.guard(): return False
        value = self.description
        if not value or value['dirty'] or not value['saved']: return False
        self.expected = dict(worker_session=self.client.session_id, request_id=None, operation_id=None,
            selection_revision=value['selection_revision'], resource_set_id=value['resource_set_id'], generation=value['generation']+1)
        req = self.client.start_operation('resource.check', runtime=runtime,
            selection_revision=value['selection_revision'],resource_set_id=value['resource_set_id'])
        if not req:
            self.expected = None; return False
        self.expected.update(request_id=req, operation_id=self.client.operation[0])
        self.progress = None; self.error = None; self.changed.emit(); return req

    def receive(self, event, request_kind):
        if request_kind != 'resource.check':
            if self.pending != event['request_id']:
                self.rejected += 1; return
            self.pending = None
            if event['type'] == 'resource.description': self.accept(event['description'])
            elif event['type'] == 'resource.error':
                if event.get('description'): self.accept(event['description'])
                self.error = event['error']; self.changed.emit()
            else: self.rejected += 1
            return
        if event['type'] == 'resource.error':
            self.error = event['error']; self.expected = None; self.changed.emit(); return
        if event['type'] == 'resource.progress':
            if event.get('owner') != self.expected: self.rejected += 1; return
            self.progress = event; self.changed.emit(); return
        detail = event.get('result', {}).get('detail') or {}
        if detail.get('owner') != self.expected:
            self.rejected += 1; self.changed.emit(); return
        self.expected = None; self.progress = None
        self.accept(detail['description'])
        if detail.get('error'):self.error=dict(message=detail['error']);self.changed.emit()
        # A check can refresh C's original descriptors without altering query or
        # translation identity. Rebind the query channel only to those facts.
        inherited = self.description.get('inherited', {}).get('dictionary')
        if inherited:
            previous = self.client.dictionary.session.bind(self.client.session_id, inherited)
            self.client.dictionary._cancel_owned(previous)
            self.client.dictionary.changed.emit(self.client.dictionary.state)

