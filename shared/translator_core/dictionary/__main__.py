"""Explicit local CLI. No implicit preparation, download, model or task access."""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .contracts import RESOURCE_VERSION, INDEX_VERSION, MAPPING_VERSION, NORMALIZATION_VERSION, DictionaryQuery
from .resources import SOURCE_INFO, ResourceCatalog, build_index, file_hash

CAP = 524288000
RESERVE = 32 * 1024 * 1024


def tree_bytes(root):
    return sum(p.stat().st_size for p in Path(root).rglob("*") if p.is_file())


def budget(root, direction):
    additional = 40960000 if direction == "zh-en" else 204800000
    if tree_bytes(root) + additional + RESERVE > CAP:
        raise ValueError("t11c_data_budget_exceeded_before_build")


def write_new(path, value):
    path = Path(path)
    temp = path.with_name(".publish-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2); handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.link(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--b-root", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--budget-root", type=Path, required=True)
    check = sub.add_parser("check"); check.add_argument("--config", type=Path, required=True)
    query = sub.add_parser("query"); query.add_argument("--config", type=Path, required=True)
    query.add_argument("--text", required=True); query.add_argument("--mode", default="auto")
    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("--config", type=Path, required=True)
    rebuild.add_argument("--direction", choices=("zh-en", "en-zh"), required=True)
    rebuild.add_argument("--output", type=Path, required=True)
    rebuild.add_argument("--budget-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        if args.output.exists() or not args.budget_root.is_dir() or not args.output.is_relative_to(args.budget_root):
            raise ValueError("new_c_resource_directory_required")
        args.output.mkdir(mode=0o700)
        specs, builds = [], {}
        for direction, info in SOURCE_INFO.items():
            budget(args.budget_root, direction)
            source = (args.b_root / "sources" / info["source_file"]).resolve()
            index = args.output / (info["resource_id"] + ".sqlite")
            result = build_index(source, index, direction, source_sha256=info["source_sha256"],
                                 source_version=info["source_version"], record_count=info["records"])
            builds[direction] = result
            specs.append(dict(info, direction=direction, source_path=os.path.relpath(source, args.output),
                index_path=index.name, index_bytes=result["index_bytes"], index_sha256=result["index_sha256"],
                index_version=INDEX_VERSION, mapping_version=MAPPING_VERSION,
                normalization_version=NORMALIZATION_VERSION))
        # The config is the atomic readiness/publication boundary; an incomplete
        # preparation directory has no usable config and is never auto-selected.
        write_new(args.output / "resources.json", dict(version=RESOURCE_VERSION, resources=specs))
        write_new(args.output / "builds.json", builds)
        print(json.dumps(builds, ensure_ascii=False))
    elif args.command == "rebuild":
        catalog = ResourceCatalog(args.config)
        spec = catalog.resources[args.direction]
        if args.output.exists() or not args.output.is_relative_to(args.budget_root):
            raise ValueError("new_c_rebuild_path_required")
        budget(args.budget_root, args.direction)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = build_index(spec["source_path"], args.output, args.direction,
            source_sha256=spec["source_sha256"], source_version=spec["source_version"], record_count=spec["records"])
        result["matches_prepared_index"] = result["index_sha256"] == spec["index_sha256"]
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "check":
        print(json.dumps(ResourceCatalog(args.config).snapshot(), ensure_ascii=False, indent=2))
    else:
        from .query import DictionaryService
        catalog = ResourceCatalog(args.config); session = str(uuid.uuid4())
        service = DictionaryService(catalog, session)
        try:
            value = DictionaryQuery.create(args.text, args.mode, 1, session, catalog.snapshot()).wire()
            print(json.dumps(service.query(value), ensure_ascii=False, indent=2))
        finally:
            service.close()


if __name__ == "__main__":
    main()
