"""Versioned, bounded JSONL over parent-owned anonymous pipes."""
import json
import uuid

PROTOCOL = {"major": 1, "minor": 11}
MAX_FRAME_BYTES = 65536
STARTUP_TIMEOUT_MS = 30000
STARTUP_ERROR_PREFIX = "B01 worker startup error: "
SHUTDOWN_TIMEOUT_MS = 35000  # capture drain, cancelled owned inference and writer release
MAX_STEPS = 40
SNAPSHOT_CHUNK_BYTES = 16384
MAX_TRANSFER_BYTES = 32 * 1024 * 1024
WORKBENCH_OPERATIONS = ('workbench.select', 'workbench.extract', 'workbench.translate')


class ProtocolError(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str):
        raise ProtocolError("invalid_identity")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise ProtocolError("invalid_identity") from exc
    return value


def encode(message):
    try:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProtocolError("invalid_json") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large")
    return raw


def decode(raw):
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ProtocolError("invalid_frame")
    try:
        message = json.loads(raw.decode("utf-8"),
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("invalid_json") from exc
    if (not isinstance(message, dict) or message.get("protocol") != PROTOCOL
            or any(type(message.get("protocol", {}).get(k)) is not int for k in PROTOCOL)):
        raise ProtocolError("unsupported_protocol")
    identifier(message.get("session_id"))
    identifier(message.get("request_id"))
    if not isinstance(message.get("type"), str):
        raise ProtocolError("invalid_type")
    if 'sequence' not in message and message['type'].startswith(('preferences.','managed.')):
        validate_settings_request(message)
    return message


def validate_settings_request(message):
    kind = message['type']
    base = {'protocol', 'session_id', 'request_id', 'type'}
    fields = {
        'preferences.describe': set(), 'preferences.stage': {'revision','generation','values'},
        'preferences.save': {'revision','generation','values'},
        'preferences.discard': {'revision','generation'},
        'preferences.reload': {'revision','generation'}, 'preferences.confirm': {'revision','generation'},
        'managed.describe': set(),
        'managed.operate': {'revision','generation','action','component','instance','confirmed',
                            'operation_id','lease_id','lease_ms'},
    }
    if kind not in fields or set(message) != base | fields[kind]:
        raise ProtocolError('invalid_settings_fields')
    if any(type(message[key]) is not int or not 0 <= message[key] < 2**53
           for key in ('revision','generation') if key in message):
        raise ProtocolError('invalid_settings_revision')
    if 'values' in message:
        from translator_core.preferences import validate_values
        try:
            validate_values(message['values'])
        except ValueError as exc:
            raise ProtocolError('invalid_preferences_values') from exc
    if kind == 'managed.operate':
        import re
        identifier(message['operation_id'])
        identifier(message['lease_id'])
        if (message['action'] not in ('prepare','repair','rollback','uninstall','recover') or
                type(message['confirmed']) is not bool or type(message['lease_ms']) is not int or
                not 100 <= message['lease_ms'] <= 30000 or
                message['component'] is not None and not isinstance(message['component'],str) or
                message['instance'] is not None and (not isinstance(message['instance'],str) or
                                                     not re.fullmatch('[0-9a-f]{32}',message['instance']))):
            raise ProtocolError('invalid_managed_operation')


def request(kind, session_id, **fields):
    return dict(protocol=PROTOCOL, session_id=session_id,
                request_id=str(uuid.uuid4()), type=kind, **fields)


class EventTracker:
    """Reject old-process callbacks and duplicate/out-of-order worker output."""
    def __init__(self, session_id):
        self.session_id = session_id
        self.sequence = 0

    def accept(self, raw):
        event = decode(raw)
        sequence = event.get("sequence")
        if (event["session_id"] != self.session_id or type(sequence) is not int
                or sequence != self.sequence + 1):
            raise ProtocolError("stale_or_out_of_order_event")
        self.sequence = sequence
        return event
