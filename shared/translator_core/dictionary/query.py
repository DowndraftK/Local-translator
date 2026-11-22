"""Bounded streaming headword retrieval and authenticated keyset pagination."""
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading

from .contracts import (DictionaryQuery, QUERY_VERSION, MAX_PAGE_BYTES, GROUP_CONTENT_BYTES,
                        MAX_RECORD_BYTES, digest, encoded)
from .mapping import normalize, units
from .resources import open_index


def cursor_binding(owner):
    return digest({k: v for k, v in owner.items() if k != "query_id"})


class DictionaryService:
    """One query owner/thread per service; at most two small SQLite caches."""
    def __init__(self, catalog, session):
        self.catalog, self.session = catalog, session
        self.secret = secrets.token_bytes(32)
        self.connections = {}
        self.connection_identities = {}
        self.thread_id = None

    def _check_owner(self):
        if self.thread_id is None:
            self.thread_id = threading.get_ident()
        if self.thread_id != threading.get_ident():
            raise RuntimeError("dictionary_connection_owner_mismatch")

    def close(self):
        self._check_owner()
        for db in self.connections.values():
            db.close()
        self.connections.clear()
        self.connection_identities.clear()

    def _connection(self, direction):
        desc = self.catalog.descriptors[direction]
        db = self.connections.get(direction)
        if db is not None and self.connection_identities.get(direction) != desc["identity"]:
            db.close(); self.connections.pop(direction); db = None
        if db is None:
            db = open_index(desc["index_path"])
            self.connections[direction] = db
            self.connection_identities[direction] = desc["identity"]
        return db

    def source_record(self, direction, record_id):
        """Explicit bounded local inspection, including all original CSV fields.

        This is not a worker frame or a query path accepting a filesystem path.
        A future UI can use the result locator and the resource description.
        """
        self._check_owner()
        if direction not in self.catalog.descriptors or type(record_id) is not int or record_id < 1:
            raise ValueError("invalid_source_locator")
        if self.catalog.state(direction) != "ready":
            raise ValueError(self.catalog.state(direction))
        row = self._connection(direction).execute("SELECT id,line_start,line_end,headword,"
            "CASE WHEN length(CAST(raw AS BLOB))<=? THEN raw ELSE NULL END AS raw FROM records WHERE id=?",
            (MAX_RECORD_BYTES, record_id)).fetchone()
        if row is None:
            raise ValueError("source_record_missing")
        if row["raw"] is None:
            raise ValueError("source_record_too_large")
        value = dict(row)
        value["resource_identity"] = self.catalog.descriptors[direction]["identity"]
        if direction == "en-zh":
            from .mapping import csv_fields
            value["csv_fields"] = csv_fields(row["raw"])
        return value

    def _cursor(self, binding, direction, position, match_mode):
        body = encoded(dict(v=1, b=binding, d=direction, p=position, m=match_mode))
        return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def _position(self, token, binding, direction):
        if not token:
            return (0, 0, 0), None
        try:
            raw, signature = token.split(".")
            body = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
            if not hmac.compare_digest(signature, hmac.new(self.secret, body, hashlib.sha256).hexdigest()):
                raise ValueError()
            value = json.loads(body)
            pos = value["p"]
            if (set(value) != {"v", "b", "d", "p", "m"} or value["v"] != 1
                    or value["b"] != binding or value["d"] != direction or value["m"] not in ("exact", "normalized")
                    or not isinstance(pos, list) or len(pos) != 3
                    or any(type(i) is not int or not 0 <= i < 2**53 for i in pos)):
                raise ValueError()
            return tuple(pos), value["m"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("invalid_cursor") from None

    def query(self, value, cancel=None):
        self._check_owner()
        cancel = cancel or threading.Event()
        query = DictionaryQuery.parse(value, self.session)
        resources = self.catalog.snapshot()
        result = dict(version=QUERY_VERSION, owner=query.owner, analysis=query.analysis,
                      status=query.analysis["status"], groups=[], page_directions=[d for d in query.analysis["directions"]
                          if not query.cursors or d in query.cursors])
        if (query.owner.get("resource_set_id") != resources["resource_set_id"] or
                query.owner.get("resource_identities") != resources["resource_identities"]):
            return dict(result, status="resource_changed", resources=resources)
        if query.analysis["directions"]:
            # Reserve actual envelope/header bytes, including JSON escaping of
            # the preserved raw query and worst-size cursors. Whitespace-heavy
            # valid inputs must paginate, not fail a late serialization check.
            skeleton = dict(result, groups=[dict(self._base(query, d), next_cursor="x" * 1024)
                                            for d in query.analysis["directions"]])
            content_budget = min(GROUP_CONTENT_BYTES,
                (MAX_PAGE_BYTES - len(encoded(skeleton)) - 64) // len(result["page_directions"]))
            for direction in query.analysis["directions"]:
                if direction in result["page_directions"]:
                    result["groups"].append(self._group(query, direction, cancel, content_budget))
                else:
                    # Loading one side must not silently restart the other side
                    # at page one. The consumer retains its existing group.
                    result["groups"].append(dict(self._base(query, direction), status="not_requested", has_more=None))
            result["status"] = "direction_ambiguous" if query.analysis["status"] == "direction_ambiguous" else "completed"
        # Detect replacement between SQL and publication. Never publish a result
        # under the old resource identity after observing such a change.
        after = self.catalog.snapshot()
        if resources["resource_set_id"] != after["resource_set_id"]:
            return dict(result, status="resource_changed", groups=[], resources=after)
        if cancel.is_set():
            result["status"] = "cancelled"
            result["groups"] = [dict(g, status="cancelled", items=[], has_more=False, next_cursor=None) for g in result["groups"]]
        if len(encoded(result)) > MAX_PAGE_BYTES:
            # Defensive failure, never silent content truncation.
            return dict(result, status="page_budget_exceeded", groups=[])
        return result

    def _base(self, query, direction):
        desc = self.catalog.descriptors[direction]
        provenance = {k: desc[k] for k in ("resource_id", "identity", "source_version", "source_sha256",
            "index_version", "index_sha256", "mapping_version", "mapping_sha256", "normalization_version") if k in desc}
        return dict(direction=direction, query=query.owner["query"], resource=provenance,
                    status="query_failed", items=[], total_count=None, has_more=False, next_cursor=None,
                    limit=query.limit, limit_unit="source_gloss_display_unit", match_mode=None)

    def _group(self, query, direction, cancel, content_budget):
        desc = self.catalog.descriptors[direction]
        base = self._base(query, direction)
        state = self.catalog.state(direction)
        if state != "ready":
            return dict(base, status=state)
        if cancel.is_set():
            return dict(base, status="cancelled")
        db = None
        try:
            position, match_mode = self._position(query.cursors.get(direction), cursor_binding(query.owner), direction)
            db = self._connection(direction)
            db.set_progress_handler(lambda: 1 if cancel.is_set() else 0, 128)
            raw = query.owner["query"]
            if match_mode is None:
                match_mode = "exact" if db.execute(
                    "SELECT 1 FROM lookup INDEXED BY lookup_key_kind WHERE key=? AND kind='exact' LIMIT 1", (raw,)).fetchone() else "normalized"
            key = raw if match_mode == "exact" else normalize(raw, direction)
            rows = db.execute("SELECT r.id,r.line_start,r.line_end,r.headword,"
                "length(CAST(r.raw AS BLOB)) AS raw_bytes,"
                "CASE WHEN length(CAST(r.raw AS BLOB))<=? THEN r.raw ELSE NULL END AS raw "
                "FROM lookup k INDEXED BY lookup_key_kind JOIN records r ON r.id=k.record_id "
                "WHERE k.key=? AND k.kind=? AND k.record_id>=? ORDER BY k.record_id",
                (MAX_RECORD_BYTES, key, match_mode, max(1, position[0])))
            used = 0
            last = position
            matched_record = False
            for row in rows:
                matched_record = True
                if cancel.is_set():
                    return dict(base, status="cancelled")
                if row["raw"] is None:
                    stream = [dict(unit_id=f'{row["id"]}:0:0', status="item_too_large", kind="record_information",
                        text=None, copy_text=None, original_record_bytes=row["raw_bytes"],
                        source_locator=dict(record_id=row["id"], line_start=row["line_start"], line_end=row["line_end"]),
                        source_group_ordinal=0, source_gloss_ordinal=0)]
                else:
                    stream = units(row, direction)
                for item in stream:
                    if cancel.is_set():
                        return dict(base, status="cancelled")
                    pos = (row["id"], item["source_group_ordinal"], item["source_gloss_ordinal"])
                    if pos <= position:
                        continue
                    size = len(encoded(item))
                    if size + 1 > content_budget:
                        # Include an explicit gap marker. The complete source
                        # remains retrievable by locator, never a cropped gloss.
                        item = {k: item[k] for k in ("unit_id", "source_locator", "source_group_ordinal", "source_gloss_ordinal")}
                        item.update(status="item_too_large", kind="source_information", text=None,
                                    copy_text=None, original_item_utf8_bytes=size)
                        size = len(encoded(item))
                    if len(base["items"]) == query.limit or used + size + 1 > content_budget:
                        base.update(has_more=True, next_cursor=self._cursor(cursor_binding(query.owner), direction, last, match_mode))
                        break
                    base["items"].append(item); used += size + 1; last = pos
                if base["has_more"]:
                    break
            rows.close()
            return dict(base, status="match" if matched_record else "no_match", match_mode=match_mode if matched_record else None)
        except ValueError as exc:
            return dict(base, status="invalid_cursor" if str(exc) == "invalid_cursor" else "resource_corrupt")
        except sqlite3.DatabaseError as exc:
            if cancel.is_set():
                return dict(base, status="cancelled")
            return dict(base, status="query_failed", error_code=type(exc).__name__)
        except OSError:
            return dict(base, status="query_failed", error_code="resource_io_error")
        finally:
            if db:
                db.set_progress_handler(None, 0)
