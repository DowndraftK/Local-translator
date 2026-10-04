"""Read-only verification of frozen bytes; not a task-store migration or writer."""
import base64
import hashlib
import json
from pathlib import Path


def verify_fixtures(root):
    root = Path(root)
    manifest = json.loads((root / "swift-manifest.json").read_text(encoding="utf-8"))
    if manifest["schema"] != 1:
        raise ValueError("future_fixture_manifest")
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("fixture_path_escape")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("fixture_hash_mismatch")
    for name, binding in manifest["recipe_bindings"].items():
        raw = (root / name).read_bytes()
        # Swift recipe identity binds the original Foundation bytes. A Python
        # json.dumps reserialization would change whitespace/numeric encoding.
        if hashlib.sha256(raw).hexdigest() != binding:
            raise ValueError("swift_binding_mismatch")
        recipe = json.loads(raw)
        if recipe["version"] == 1 and "contextVersion" in recipe:
            raise ValueError("legacy_context_was_added")
        if recipe["options"]["num_ctx"] != 8192 or recipe["options"]["num_predict"] != 4096:
            raise ValueError("budget_changed")
    snapshots = []
    for name in ("current.json", "previous.json"):
        path = root / "text-task" / manifest["task_id"] / name
        envelope = json.loads(path.read_bytes())
        if envelope["schema"] != 1:
            raise ValueError("future_snapshot_schema")
        payload = base64.b64decode(envelope["payload"], validate=True)
        if hashlib.sha256(payload).hexdigest() != envelope["checksum"]:
            raise ValueError("payload_checksum_mismatch")
        task = json.loads(payload)
        if task["id"] != manifest["task_id"]:
            raise ValueError("snapshot_identity_mismatch")
        asset = task["asset"]
        if Path(asset["filename"]).name != asset["filename"]:
            raise ValueError("asset_path_escape")
        source = path.parent / asset["filename"]
        raw_source = source.read_bytes()
        if len(raw_source) != asset["byteCount"] or hashlib.sha256(raw_source).hexdigest() != asset["fingerprint"]:
            raise ValueError("source_identity_mismatch")
        if task["input"].encode("utf-8") != raw_source:
            raise ValueError("source_text_changed")
        successful = [s for s in task["text"]["segments"] if s["state"] == "completed"]
        expected_binding = manifest["recipe_bindings"]["swift-recipe-v1-en-zh.json"]
        if any(s["result"]["configurationBinding"] != expected_binding for s in successful):
            raise ValueError("successful_result_binding_changed")
        snapshots.append(task)
    if snapshots[0]["text"] != snapshots[1]["text"] or snapshots[0]["revision"] <= snapshots[1]["revision"]:
        raise ValueError("snapshot_prefix_or_revision_changed")
    subtitle = json.loads((root / "python-recipes.json").read_text(encoding="utf-8"))
    for item in subtitle["recipes"]:
        raw = json.dumps(item["configuration"], sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode()
        if hashlib.sha256(raw).hexdigest() != item["binding"]:
            raise ValueError("subtitle_binding_mismatch")
    return {"swift_recipes": len(manifest["recipe_bindings"]), "snapshots": len(snapshots),
            "subtitle_recipes": len(subtitle["recipes"]), "writes": 0, "inference": False}
