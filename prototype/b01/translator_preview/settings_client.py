"""Preferences and managed resources over the existing worker and long-operation lease."""
from copy import deepcopy

from PySide6.QtCore import QObject, Signal

from translator_core.preferences import validate_description as validate_preferences, validate_values
from translator_core.resource_lifecycle import validate_description as validate_managed


class SettingsClient(QObject):
    changed = Signal()

    def __init__(self, client, namespace):
        super().__init__(client)
        self.client = client
        self.namespace = namespace
        self.description = self.last_description = None
        self.pending = self.expected = None
        self.error = self.progress = None
        self.rejected = 0
        self.guard = lambda: True

    def disconnect(self):
        self.description = self.pending = self.expected = self.progress = None
        self.changed.emit()

    def accept(self, value):
        (validate_preferences if self.namespace == 'preferences' else validate_managed)(value)
        self.description = deepcopy(value)
        self.last_description = deepcopy(value)
        self.error = None
        self.changed.emit()

    def configure(self, diagnostics):
        value = diagnostics.get(self.namespace)
        if value is not None:
            self.accept(value)

    def describe(self):
        if self.pending or self.client.state not in ('ready', 'running', 'cancelling'):
            return False
        self.pending = self.client.send(self.namespace+'.describe')
        return self.pending

    def action(self, action, **fields):
        if self.pending or self.client.state != 'ready' or self.client.task_request_pending() or not self.guard():
            return False
        value = self.description
        if not value:
            return False
        if self.namespace == 'preferences' and 'values' in fields:
            try:
                validate_values(fields['values'])
            except (TypeError, ValueError) as exc:
                self.error = dict(code=getattr(exc, 'code', 'preferences_invalid'),
                                  message=str(exc), retryable=True)
                self.changed.emit()
                return False
        self.pending = self.client.send(self.namespace+'.'+action, revision=value['revision'],
                                       generation=value['generation'], **fields)
        self.error = None
        self.changed.emit()
        return self.pending

    def operate(self, action, component=None, instance=None, confirmed=False):
        if self.namespace != 'managed' or self.pending or self.expected or not self.guard():
            return False
        value = self.description
        if not value:
            return False
        self.expected = dict(worker_session=self.client.session_id, request_id=None, operation_id=None,
                             configuration_revision=self.client.preferences.description['revision'],
                             selection_revision=self.client.resources.description['selection_revision'],
                             resource_set_id=self.client.resources.description['resource_set_id'],
                             generation=value['generation']+1)
        request = self.client.start_operation('managed.operate', action=action, component=component,
                                             instance=instance, confirmed=confirmed,
                                             revision=value['revision'], generation=value['generation'])
        if not request:
            self.expected = None
            return False
        self.expected.update(request_id=request, operation_id=self.client.operation[0])
        self.progress = self.error = None
        self.changed.emit()
        return request

    def receive(self, event, request_kind):
        if request_kind != 'managed.operate':
            if self.pending != event['request_id']:
                self.rejected += 1
                return
            self.pending = None
            if event.get('description') is not None:
                self.accept(event['description'])
            if event['type'].endswith('.error'):
                self.error = event['error']
                self.changed.emit()
            return
        if event['type'] == 'managed.error':
            if self.expected and event['request_id'] == self.expected['request_id']:
                self.expected = self.progress = None
                if event.get('description'):
                    self.accept(event['description'])
                self.error = event['error']
                self.changed.emit()
            else:
                self.rejected += 1
            return
        value = event.get('result', {}).get('detail') if event['type'] == 'operation.finished' else event
        if not value or value.get('owner') != self.expected:
            self.rejected += 1
            return
        if event['type'] == 'managed.progress':
            self.progress = event
        else:
            self.expected = self.progress = None
            self.accept(value['description'])
            error = (value.get('transaction') or {}).get('error')
            if error:
                self.error = error
            elif value.get('error'):
                self.error = dict(message=value['error'])
            if value.get('resources'):
                self.client.resources.accept(value['resources'])
            if value.get('dictionary'):
                self.client.dictionary.configure(dict(capabilities={'dictionary':'dictionary-query-v1'},
                                                      dictionary=value['dictionary']))
        self.changed.emit()
