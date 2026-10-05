"""Versioned, bounded JSONL over parent-owned anonymous pipes."""
import json
import uuid

PROTOCOL = {"major": 1, "minor": 3}
MAX_FRAME_BYTES = 65536
STARTUP_TIMEOUT_MS = 30000
SHUTDOWN_TIMEOUT_MS = 10000  # current native page terminate/wait (5s) plus finite tail
MAX_STEPS = 40
SNAPSHOT_CHUNK_BYTES = 16384
MAX_TRANSFER_BYTES = 32 * 1024 * 1024


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
    return message


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
