"""Schema2 separates raw recognition, effective revision, drafts and human review."""
import math
import uuid
from copy import deepcopy
from . import CoreError
from .contracts import MAX_INPUT_BYTES, task_id, trim

MAX_OBSERVATIONS_PAGE = 4096
MAX_OBSERVATIONS_TASK = 16384
COORDINATES = {"normalized-bottom-left", "pixel-top-left"}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_raw(raw):
    if not isinstance(raw, dict): raise CoreError("原始OCR字段无效。")
    task_id(raw.get("rawRevision"))
    if (not isinstance(raw.get("rawText"), str) or not isinstance(raw.get("backend"), str) or
            not raw["backend"] or raw.get("recognitionComplete") is not True or
            not isinstance(raw.get("operation"), str) or not raw["operation"]):
        raise CoreError("原始OCR未完整确认或身份缺失。")
    observations = raw.get("observations")
    if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS_PAGE:
        raise CoreError("观察项超过单页4096项保护。", "resource_limit")
    w, h = raw.get("pixelWidth"), raw.get("pixelHeight")
    if (type(w) is not int or type(h) is not int or min(w, h) <= 0 or
            max(w, h) > 3000 or w * h > 9_000_000):
        raise CoreError("原始渲染像素超限。", "resource_limit")
    crop = raw.get("cropBox")
    if (raw.get("rotation") not in (0, 90, 180, 270) or not isinstance(crop, list) or
            len(crop) != 4 or not all(finite(v) for v in crop) or min(crop[2:]) <= 0 or
            not (raw.get('evidenceOrigin')=='schema1-preserved' and raw.get('scale') is None or
                 finite(raw.get("scale")) and 0 < raw["scale"] <= 3) or
            raw.get("coordinateSystem") not in COORDINATES or raw.get("orientation") != "up"):
        raise CoreError("原始渲染/坐标规则无效。")
    if (not isinstance(raw.get("languages"), list) or not raw["languages"] or
            any(not isinstance(v, str) for v in raw["languages"]) or
            raw.get("textAngle") is not None and not finite(raw["textAngle"])):
        raise CoreError("原始语言/文字角度字段无效。")
    ids = set(); observation_bytes = 0
    for n, o in enumerate(observations):
        if (not isinstance(o, dict) or type(o.get("order")) is not int or o["order"] != n or
                not isinstance(o.get("text"), str) or not isinstance(o.get("id"), str) or
                not 0 < len(o["id"]) <= 256 or o["id"] in ids):
            raise CoreError("原始识别顺序/身份无效。")
        ids.add(o["id"]); observation_bytes += len(o["text"].encode())
        c, r = o.get("confidence"), o.get("rect")
        if c is not None and (not finite(c) or not 0 <= c <= 1): raise CoreError("置信度无效。")
        if r is not None:
            if (not isinstance(r, list) or len(r) != 4 or not all(finite(v) for v in r) or
                    min(r) < 0 or min(r[2:]) < 0): raise CoreError("原始坐标无效。")
            bw, bh = (1, 1) if raw["coordinateSystem"] == "normalized-bottom-left" else (w, h)
            if r[0] + r[2] > bw + .001 or r[1] + r[3] > bh + .001:
                raise CoreError("原始坐标超出渲染图像。")
    return len(raw["rawText"].encode()), observation_bytes, len(observations)


def validate_ocr(task, pages):
    d = task["document"]
    if (d["mode"] != "ocr" or not d["isPDF"] or not isinstance(d.get("ocrBackend"), dict) or
            any(p["mode"] != "ocr" for p in pages.values())):
        raise CoreError("schema2仅用于显式PDF OCR任务。")
    raw_bytes = observation_bytes = observation_count = 0
    for p in pages.values():
        source = p.get("effectiveSource")
        if (not isinstance(source, dict) or source.get("kind") not in
                ("unobtained", "ocr", "ocr-empty", "manual-correction", "manual-clear", "manual-input", "inherited-ocr")):
            raise CoreError("有效来源身份缺失。", "source_revision_mismatch")
        if p["reviewed"] and p.get("reviewRevision") != p["revision"]:
            raise CoreError("人工核对不属于有效修订。", "source_revision_mismatch")
        if not p["reviewed"] and p.get("reviewRevision") is not None:
            raise CoreError("未核对页不能带核对修订。")
        raw = p.get("ocr")
        if raw is None:
            if source.get("rawRevision") is not None or source["kind"] in ("ocr", "ocr-empty", "inherited-ocr"):
                raise CoreError("没有原始OCR却带识别来源。", "source_revision_mismatch")
            continue
        a, b, c = validate_raw(raw); raw_bytes += a; observation_bytes += b; observation_count += c
        if source.get("rawRevision") != raw["rawRevision"]:
            raise CoreError("有效原文不属于此原始OCR。", "source_revision_mismatch")
        if source["kind"] in ("ocr", "ocr-empty") and p["text"] != raw["rawText"]:
            raise CoreError("未校正有效原文与原始OCR不一致。", "source_revision_mismatch")
    draft_bytes = 0
    for number, draft in task["drafts"].items():
        p = pages.get(int(number)) if str(number).isdigit() else None
        if (p is None or not isinstance(draft, dict) or draft.get("baseRevision") != p["revision"] or
                not isinstance(draft.get("text"), str)):
            raise CoreError("草稿不属于有效页修订。", "draft_revision_mismatch")
        task_id(draft.get("id")); draft_bytes += len(draft["text"].encode())
    if (max(raw_bytes, observation_bytes, draft_bytes) > MAX_INPUT_BYTES or
            observation_count > MAX_OBSERVATIONS_TASK):
        raise CoreError("原始文字/观察项文字/草稿累计超过1MiB，或任务观察项超过16384项。", "resource_limit")


def install_raw(page, result, operation):
    raw = deepcopy(result.get("ocr"))
    if not isinstance(raw, dict) or raw.get("recognitionComplete") is not True:
        raise CoreError("未获得完整识别响应，不提交部分页。", "incomplete_ocr")
    raw.update(rawRevision=str(uuid.uuid4()).upper(), operation=operation, rawText=result["text"])
    validate_raw(raw)
    page.update(ocr=raw, revision=str(uuid.uuid4()).upper(), reviewed=False, reviewRevision=None)
    page["effectiveSource"] = dict(kind="ocr" if trim(result["text"]) else "ocr-empty", rawRevision=raw["rawRevision"])
