"""Explicit local preparation/inspection; immutable query resources, no network."""
import csv
import hashlib
import json
import os
import sqlite3
import time
import uuid
from copy import deepcopy
from pathlib import Path

from . import DICTIONARY_CORE_VERSION
from .contracts import (RESOURCE_VERSION, INDEX_VERSION, MAPPING_VERSION, NORMALIZATION_VERSION,
                        MAX_RECORD_BYTES, digest, encoded)
from .mapping import CC, CSV_HEADER, normalize
from .rules import DIRECTIONS

SOURCE_INFO = {
    "zh-en": dict(resource_id="cc-cedict", source_version="v1-ts-utf8-2026-10-21T05:46:59Z",
        source_file="cedict_ts.u8", source_sha256="f6b2248b589a7f231952bbcb86db5206c089c69c9de06ded0f2a5569e962ec64",
        source_bytes=9866261, records=125295,
        origin="https://www.mdbg.net/chinese/dictionary?lang=en&page=cc-cedict",
        license="CC BY-SA 4.0", distribution_status="conditions_recorded_not_distributed",
        notice="CC-CEDICT/MDBG; CEDICT Copyright 1997, 1998 Paul Andrew Denisowski; retain attribution/license and applicable ShareAlike obligations.",
        known_content_limits="B finite coverage/errors retained; no Chinese hints or candidate POS; literal/figurative definitions are information."),
    "en-zh": dict(resource_id="ecdict", source_version="bc015ed2e24a7abef49fc6dbbb7fe32c1dadaf8b-base-csv",
        source_file="ecdict.csv", source_sha256="1a6947e04785db63613a92e14903cdae7954f7e84860b10e68e5c7cbb3f9c3cf",
        source_bytes=65933428, records=770611,
        origin="https://github.com/skywind3000/ECDICT/tree/bc015ed2e24a7abef49fc6dbbb7fe32c1dadaf8b",
        license="Repository MIT (Copyright 2025 Linwei); data upstream redistribution chain unresolved",
        distribution_status="upstream_data_rights_unresolved_local_use_authorized",
        notice="Keep repository MIT notice; software license alone does not establish CSV upstream data rights.",
        known_content_limits="B six top3 ordinary errors and extra run/ran serious data defect retained. POS field empty; omit absent phonetic/translation. No content correction."),
}


def file_hash(path, cancel=None):
    with Path(path).open("rb") as handle:
        hashed = hashlib.sha256()
        while block := handle.read(1024 * 1024):
            if cancel is not None and cancel.is_set():
                from ..resources import ResourceError
                raise ResourceError('check_cancelled', '词典资源检查已取消。', retryable=True)
            hashed.update(block)
        return hashed.hexdigest()


def file_token(path):
    st = Path(path).stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def open_index(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=.05)
    db.row_factory = sqlite3.Row
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * MAX_RECORD_BYTES)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA cache_size=-2048")
    db.execute("PRAGMA temp_store=MEMORY")
    return db


def read_meta(db):
    rows = db.execute("SELECT key,value FROM meta LIMIT 17").fetchall()
    if len(rows) > 16:
        raise ValueError("resource_incompatible")
    return {r["key"]: json.loads(r["value"]) for r in rows}


class ResourceCatalog:
    """Fully verify once at prepare/start/check; ordinary queries only stat.

    Connections are opened by the owning query thread, never shared with the
    translation store. Configuration paths are explicit, never query supplied.
    """
    def __init__(self, config_path=None, forbidden_roots=(), *, lazy=False):
        self.strict_paths = lazy
        self.config_path = Path(os.path.abspath(config_path)) if config_path and lazy else Path(config_path).resolve() if config_path else None
        self.resources = {}
        self.forbidden_roots = tuple(Path(root).resolve() for root in forbidden_roots)
        self.config_error = None
        if self.config_path:
            try:
                if self.config_path.stat().st_size > 32768:
                    raise ValueError("resource_incompatible")
                if lazy:
                    from ..resources import safe_read,json_value
                    config=json_value(safe_read(self.config_path,32768))
                else:
                    config = json.loads(self.config_path.read_bytes())
                if config.get("version") != RESOURCE_VERSION or not isinstance(config.get("resources"), list):
                    raise ValueError("resource_incompatible")
                for spec in config["resources"]:
                    direction = spec["direction"]
                    if direction not in DIRECTIONS or direction in self.resources:
                        raise ValueError("resource_incompatible")
                    spec = dict(spec)
                    for key in ("source_path", "index_path"):
                        path=Path(os.path.abspath(self.config_path.parent / spec[key]))
                        if lazy:
                            from ..resources import no_links
                            no_links(path)
                        spec[key] = str(path if lazy else path.resolve())
                    self.resources[direction] = spec
            except FileNotFoundError:
                self.config_error = "resource_missing"
            except (ValueError, KeyError, TypeError, OSError):
                self.config_error = "resource_incompatible"
                self.resources.clear()
        if lazy:
            self.descriptors = {}
            for direction in DIRECTIONS:
                spec = self.resources.get(direction)
                desc = dict(spec) if spec else dict(direction=direction, resource_id=SOURCE_INFO[direction]['resource_id'])
                desc.update(runtime_status=self.config_error or 'resource_unchecked', identity=None)
                self.descriptors[direction] = desc
        else:
            self.refresh()

    def refresh(self, cancel=None):
        # Publish the complete two-direction snapshot only after the bounded
        # active check finishes; ordinary queries continue on the old snapshot.
        descriptions = {d: self._inspect(d, cancel) for d in DIRECTIONS}
        self.descriptors = descriptions

    def restore(self, snapshot):
        """Reuse a T09-A check receipt by exact C identity and current tokens."""
        if (not isinstance(snapshot,dict) or snapshot.get('version') != RESOURCE_VERSION or
                not isinstance(snapshot.get('resources'),list) or len(snapshot['resources'])!=2): return False
        prepared = {}
        for desc in snapshot.get('resources', []):
            if not isinstance(desc,dict):return False
            direction = desc.get('direction'); spec = self.resources.get(direction)
            if not spec or direction in prepared or desc.get('runtime_status') != 'ready': return False
            if any(desc.get(k) != v for k,v in spec.items()): return False
            if (desc.get('identity') != digest({k:v for k,v in desc.items() if k!='identity'}) or
                    desc.get('mapping_version') != MAPPING_VERSION or
                    desc.get('mapping_sha256') != file_hash(Path(__file__).with_name('mapping.py'))): return False
            try:
                if set(desc['file_tokens']) != {'source','index'}: return False
                for prefix, expected in desc['file_tokens'].items():
                    if file_token(desc[prefix+'_path']) != expected: return False
            except (OSError, KeyError, TypeError): return False
            prepared[direction] = deepcopy(desc)
        if set(prepared) != set(DIRECTIONS): return False
        self.descriptors = prepared
        return True

    def _inspect(self, direction, cancel=None):
        spec = self.resources.get(direction)
        if not spec:
            return dict(direction=direction, runtime_status=self.config_error or "resource_missing",
                        resource_id=SOURCE_INFO[direction]["resource_id"], identity=None,
                        distribution_status="not_loaded")
        desc = dict(spec)
        desc.update(runtime_status="ready", core_version=DICTIONARY_CORE_VERSION,
                    mapping_version=MAPPING_VERSION, mapping_sha256=file_hash(Path(__file__).with_name("mapping.py")))
        db = None
        try:
            if any(Path(spec[key]).is_relative_to(root) for root in self.forbidden_roots
                   for key in ("source_path", "index_path")):
                raise ValueError("resource_incompatible")
            if (spec.get("index_version") != INDEX_VERSION or spec.get("mapping_version") != MAPPING_VERSION
                    or spec.get("normalization_version") != NORMALIZATION_VERSION):
                raise ValueError("resource_incompatible")
            tokens = {}
            for prefix in ("source", "index"):
                path = Path(spec[prefix + "_path"])
                if self.strict_paths:
                    from ..resources import no_links
                    no_links(path)
                if not path.is_file():
                    raise FileNotFoundError()
                token = file_token(path)
                if token[2] != spec[prefix + "_bytes"] or file_hash(path, cancel) != spec[prefix + "_sha256"]:
                    raise ValueError("resource_corrupt")
                if token != file_token(path):
                    raise ValueError("resource_corrupt")
                tokens[prefix] = token
            db = open_index(spec["index_path"])
            if cancel is not None:db.set_progress_handler(lambda:1 if cancel.is_set() else 0,1000)
            meta = read_meta(db)
            expected = dict(schema=INDEX_VERSION, normalization=NORMALIZATION_VERSION,
                resource_id=spec["resource_id"], direction=direction,
                source_sha256=spec["source_sha256"], source_version=spec["source_version"],
                header=CSV_HEADER if direction == "en-zh" else None)
            if meta != expected:
                raise ValueError("resource_incompatible")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("resource_corrupt")
            # Verify exact tables/columns and required lookup index, not just a
            # meta string. Queries never perform a full scan to repair it.
            for table, names in (("records", ["id", "line_start", "line_end", "headword", "raw"]),
                                 ("lookup", ["key", "kind", "record_id"])):
                if [r[1] for r in db.execute(f"PRAGMA table_info({table})")] != names:
                    raise ValueError("resource_incompatible")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='lookup_key_kind'").fetchone():
                raise ValueError("resource_incompatible")
            if [r[2] for r in db.execute("PRAGMA index_info(lookup_key_kind)")] != ["key", "kind", "record_id"]:
                raise ValueError("resource_incompatible")
            desc["file_tokens"] = tokens
        except FileNotFoundError:
            desc["runtime_status"] = "resource_missing"
        except (sqlite3.DatabaseError, OSError):
            desc["runtime_status"] = "resource_corrupt"
        except (ValueError, KeyError, TypeError) as exc:
            if getattr(exc, 'code', None) == 'check_cancelled': raise
            desc["runtime_status"] = "resource_corrupt" if str(exc) == "resource_corrupt" else "resource_incompatible"
        finally:
            if db:
                db.close()
        if cancel is not None and cancel.is_set():
            from ..resources import ResourceError
            raise ResourceError('check_cancelled','词典资源检查已取消。',retryable=True)
        desc["identity"] = digest(desc)
        return desc

    def snapshot(self):
        current = {}
        for d in DIRECTIONS:
            desc = deepcopy(self.descriptors[d]); state = self.state(d)
            if state != desc["runtime_status"]:
                desc["runtime_status"] = state
                desc["identity"] = digest(dict(prepared_identity=desc["identity"], runtime_status=state))
            current[d] = desc
        identities = {d: current[d]["identity"] for d in DIRECTIONS}
        return dict(version=RESOURCE_VERSION, resource_identities=identities,
                    resource_set_id=digest(identities), resources=[current[d] for d in DIRECTIONS])

    def state(self, direction):
        desc = self.descriptors[direction]
        if desc["runtime_status"] != "ready":
            return desc["runtime_status"]
        try:
            for prefix, token in desc["file_tokens"].items():
                if file_token(desc[prefix + "_path"]) != token:
                    return "resource_corrupt"
        except FileNotFoundError:
            return "resource_missing"
        except OSError:
            return "resource_corrupt"
        return "ready"


class RecordedLines:
    def __init__(self, handle):
        self.handle, self.parts = handle, []
    def __iter__(self):
        return self
    def __next__(self):
        raw = next(self.handle); self.parts.append(raw); return raw
    def take(self):
        raw = "".join(self.parts); self.parts.clear(); return raw


def build_index(source, destination, direction, *, source_sha256, source_version, record_count=None, cancel=None, deadline=None):
    """Explicit, offline, new-file-only deterministic rebuild from raw source.

    The caller chooses a new C directory and budgets its peak. Source bytes are
    never copied or modified. No B experiment implementation is imported.
    """
    source, destination = Path(source), Path(destination)
    if direction not in DIRECTIONS or destination.exists() or not destination.parent.is_dir():
        raise ValueError("new_index_path_required")
    if file_hash(source, cancel) != source_sha256:
        raise ValueError("source_hash_mismatch")
    source_token = file_token(source)
    temp = destination.with_name(".prepare-" + uuid.uuid4().hex + ".sqlite")
    db = sqlite3.connect(temp)
    started = time.perf_counter()
    count = 0
    try:
        db.executescript("PRAGMA page_size=4096; PRAGMA temp_store=MEMORY; PRAGMA journal_mode=OFF;"
            "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE records(id INTEGER PRIMARY KEY,line_start INTEGER,line_end INTEGER,headword TEXT,raw TEXT);"
            "CREATE TABLE lookup(key TEXT,kind TEXT,record_id INTEGER);")
        db.execute("PRAGMA max_page_count=" + ("10000" if direction == "zh-en" else "50000"))
        with source.open(encoding="utf-8", newline="") as handle:
            if direction == "zh-en":
                iterator = ((i, i, raw, None) for i, raw in enumerate(handle, 1) if not raw.startswith("#"))
            else:
                recorded = RecordedLines(handle); reader = csv.reader(recorded, strict=True)
                if next(reader) != CSV_HEADER:
                    raise ValueError("invalid_csv_header")
                recorded.take()
                def rows():
                    end = reader.line_num
                    for values in reader:
                        start, end = end + 1, reader.line_num
                        yield start, end, recorded.take(), values
                iterator = rows()
            for start, end, raw, values in iterator:
                if cancel is not None:
                    from ..resources import stop_check
                    stop_check(cancel, deadline or started + 300)
                if not raw.strip():
                    raise ValueError("unexpected_blank_record")
                if len(raw.encode()) > MAX_RECORD_BYTES:
                    raise ValueError("source_record_too_large")
                if direction == "zh-en":
                    match = CC.fullmatch(raw.rstrip("\r\n"))
                    if not match:
                        raise ValueError("invalid_cedict_record")
                    traditional, headword, _, _ = match.groups()
                    keys = [(headword, "exact"), (traditional, "exact")]
                else:
                    if len(values) != len(CSV_HEADER) or not values[0]:
                        raise ValueError("invalid_ecdict_record")
                    headword = values[0]; keys = [(headword, "exact")]
                count += 1
                db.execute("INSERT INTO records VALUES(?,?,?,?,?)", (count, start, end, headword, raw))
                keys = set(keys + [(normalize(k, direction), "normalized") for k, _ in keys])
                db.executemany("INSERT INTO lookup VALUES(?,?,?)", [(k, kind, count) for k, kind in sorted(keys)])
        if file_token(source) != source_token or record_count is not None and count != record_count:
            raise ValueError("source_changed_or_count_mismatch")
        db.execute("CREATE INDEX lookup_key_kind ON lookup(key,kind,record_id)")
        meta = dict(schema=INDEX_VERSION, normalization=NORMALIZATION_VERSION, direction=direction,
                    resource_id=SOURCE_INFO[direction]["resource_id"], source_sha256=source_sha256,
                    source_version=source_version, header=CSV_HEADER if direction == "en-zh" else None)
        db.executemany("INSERT INTO meta VALUES(?,?)", [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("index_integrity_failed")
        db.close(); db = None
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        if destination.exists():
            raise ValueError("refuse_index_overwrite")
        # link is an atomic no-clobber publication on the same filesystem.
        os.link(temp, destination)
        return dict(records=count, index_bytes=destination.stat().st_size,
                    index_sha256=file_hash(destination), seconds=time.perf_counter() - started,
                    integrity_check="ok", deterministic_meta=meta)
    finally:
        if db:
            db.close()
        temp.unlink(missing_ok=True)
