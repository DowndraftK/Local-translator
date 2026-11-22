"""Versioned bounded dictionary values. No translation snapshot fields."""
import hashlib
import json
import uuid
from dataclasses import dataclass

from .rules import analyze_query, identity

QUERY_VERSION = "dictionary-query-v1"
RESOURCE_VERSION = "dictionary-resources-v1"
INDEX_VERSION = "t11c-index-v1"
MAPPING_VERSION = "t11c-source-groups-v2"
NORMALIZATION_VERSION = "t11b-normalization-v1"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_PAGE_BYTES = 49152
GROUP_CONTENT_BYTES = 18000
MAX_RECORD_BYTES = 65536
MAX_CURSOR_BYTES = 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str):
        raise ValueError("invalid_query_identity")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise ValueError("invalid_query_identity") from exc
    return value


@dataclass(frozen=True)
class DictionaryQuery:
    owner: dict
    analysis: dict
    limit: int
    cursors: dict

    def wire(self):
        return dict(version=QUERY_VERSION, owner=self.owner, analysis=self.analysis,
                    limit=self.limit, cursors=self.cursors)

    @classmethod
    def create(cls, raw, mode, revision, session, resources, *, query_id=None,
               limit=DEFAULT_LIMIT, cursors=None):
        analysis = analyze_query(raw, mode).wire()
        return cls(dict(worker_session=session, query_id=query_id or str(uuid.uuid4()),
                        input_revision=revision, query=raw, direction_mode=mode,
                        rule_identity=identity(), detected=analysis["detected"],
                        directions=analysis["directions"], resource_set_id=resources["resource_set_id"],
                        resource_identities=resources["resource_identities"]), analysis, limit, cursors or {})

    @classmethod
    def parse(cls, value, session):
        if not isinstance(value, dict) or value.get("version") != QUERY_VERSION:
            raise ValueError("unsupported_contract")
        owner = value.get("owner")
        if not isinstance(owner, dict) or set(owner) != {"worker_session", "query_id", "input_revision", "query",
                "direction_mode", "rule_identity", "detected", "directions", "resource_set_id", "resource_identities"}:
            raise ValueError("invalid_query_identity")
        identifier(owner.get("query_id"))
        if owner.get("worker_session") != session:
            raise ValueError("worker_session_mismatch")
        if type(owner.get("input_revision")) is not int or not 0 <= owner["input_revision"] < 2**53:
            raise ValueError("invalid_input_revision")
        raw = owner.get("query")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8192:
            raise ValueError("invalid_query")
        if owner.get("rule_identity") != identity():
            raise ValueError("unsupported_rules")
        analysis = analyze_query(raw, owner.get("direction_mode")).wire()
        # All direction/count claims are recomputed, never used as authority.
        if (value.get("analysis") != analysis or owner.get("detected") != analysis["detected"]
                or owner.get("directions") != analysis["directions"]):
            raise ValueError("analysis_mismatch")
        limit, cursors = value.get("limit"), value.get("cursors")
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise ValueError("invalid_limit")
        if (not isinstance(cursors, dict) or any(d not in analysis["directions"] for d in cursors)
                or any(not isinstance(c, str) or len(c.encode()) > MAX_CURSOR_BYTES for c in cursors.values())):
            raise ValueError("invalid_cursor")
        # Keep the owner small even for hostile clients.
        if len(encoded(owner)) > 12288:
            raise ValueError("invalid_query_identity")
        return cls(owner, analysis, limit, cursors)
