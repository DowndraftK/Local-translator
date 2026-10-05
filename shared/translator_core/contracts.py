"""Foundation recipe identity and schema1 text semantics, kept out of the UI."""
import base64
import hashlib
import json
import math
import uuid
from copy import deepcopy
from pathlib import Path

from . import CoreError

SOURCE_BYTES = 2048
MAX_INPUT_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
STATES = {"pending", "processing", "completed", "failed", "stopped"}
LABELS = {"pending": "尚未翻译", "processing": "处理中（尚未完成）", "completed": "已完成",
          "failed": "翻译失败", "stopped": "已停止（本段中断）"}
FOUNDATION_WHITESPACE = "\t\n\v\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u2028\u2029\u202f\u205f\u3000"


def trim(value):
    return value.strip(FOUNDATION_WHITESPACE)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def foundation_json(value):
    """Same formatting as the frozen Foundation JSONOutput recipe fixtures.

    This is a compatibility encoder, not Python's compact subtitle dialect.
    Saved v1 stays absent contextVersion; unknown fields are not invented.
    """
    def scalar(item):
        if isinstance(item, float):
            if not math.isfinite(item):
                raise CoreError("JSON不能保存非有限数值。")
            if item == 0 and math.copysign(1, item) < 0:
                return "-0"
            return str(int(item)) if item.is_integer() else json.dumps(item, allow_nan=False)
        return json.dumps(item, ensure_ascii=False, allow_nan=False)

    def render(item, depth):
        prefix = "  " * (depth + 1)
        if isinstance(item, dict) and item:
            return "{\n" + ",\n".join(prefix + scalar(k) + " : " + render(v, depth + 1)
                for k, v in sorted(item.items())) + "\n" + "  " * depth + "}"
        if isinstance(item, list) and item:
            return "[\n" + ",\n".join(prefix + render(v, depth + 1) for v in item) + "\n" + "  " * depth + "]"
        return scalar(item)
    return render(value, 0).encode("utf-8")


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CoreError("JSON字段重复。")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as exc:
        raise CoreError("保存数据不是有效UTF-8 JSON。") from exc


def task_id(value):
    try:
        return str(uuid.UUID(value)).upper()
    except (ValueError, TypeError, AttributeError) as exc:
        raise CoreError("任务UUID无效。") from exc


def recipe_binding(recipe):
    return sha(foundation_json(recipe))


def default_recipe(direction, version=2):
    if direction not in ("en-zh", "zh-en") or version not in (1, 2):
        raise CoreError("请选择英中或中英方向。")
    assets = Path(__file__).parent / "assets"
    if not assets.is_dir():
        assets = Path(__file__).resolve().parents[2] / "fixtures/b01"
    raw = (assets / f"swift-recipe-v{version}-{direction}.json").read_bytes()
    recipe = strict_json(raw)
    if foundation_json(recipe) != raw:
        raise CoreError("冻结的翻译配置字节不匹配。")
    return deepcopy(recipe)


def validate_recipe(recipe):
    if not isinstance(recipe, dict) or not isinstance(recipe.get("options"), dict):
        raise CoreError("翻译配置字段无效。")
    options = recipe.get("options", {})
    version = recipe.get("version")
    if not (type(version) is int and (version == 1 and "contextVersion" not in recipe or
            version == 2 and recipe.get("contextVersion") == "none-v1" and
            recipe.get("promptProfile") == "hy-mt2-faithful-v2")):
        raise CoreError("配置版本不兼容；可阅读/导出，请另建任务。", "recipe_incompatible")
    if (recipe.get("splitterVersion") not in ("natural-language-utf8-v1", "qt6102-unicode-utf8-v1") or
            recipe.get("direction") not in ("en-zh", "zh-en") or
            options.get("num_ctx") != 8192 or options.get("num_predict") != 4096 or
            not all(type(v) in (int, float) and math.isfinite(v) for v in options.values())):
        raise CoreError("翻译配置或容量不兼容。", "recipe_incompatible")
    if (not isinstance(recipe.get("model"), str) or not isinstance(recipe.get("userPrefix"), str) or
            recipe.get("systemPrompt") is not None and not isinstance(recipe["systemPrompt"], str)):
        raise CoreError("翻译配置字段无效。")
    if recipe.get("keepAlive") != "5m":
        raise CoreError("此预览只接续既有keep_alive5m配置。", "recipe_incompatible")
    digest = recipe.get("modelDigest")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise CoreError("缺少可核对的模型digest；可阅读/导出，请另建任务。", "model_identity_missing")


def validate_task(task):
    if not isinstance(task, dict) or type(task.get("revision")) is not int or not 0 < task["revision"] < 2**64:
        raise CoreError("任务序号无效。")
    task_id(task.get("id"))
    source, recipe, text = task.get("input"), task.get("configuration"), task.get("text")
    if not isinstance(source, str) or len(source.encode()) > MAX_INPUT_BYTES or not isinstance(recipe, dict):
        raise CoreError("文字任务超出预览容量或字段无效。")
    if (not isinstance(task.get("name"), str) or not isinstance(task.get("drafts"), dict) or
            any(type(task.get(k)) not in (int, float) or not math.isfinite(task[k]) for k in ("created", "updated"))):
        raise CoreError("任务名称、日期或草稿字段无效。")
    if (not isinstance(recipe.get("model"), str) or recipe.get("direction") not in ("en-zh", "zh-en") or
            not isinstance(recipe.get("options"), dict) or not isinstance(recipe.get("userPrefix"), str)):
        raise CoreError("保存配置缺少必需字段。")
    if task.get("kind") == "文档":
        from .documents import validate_document_task
        validate_document_task(task)
        return
    if task.get("kind") != "文字" or task.get("document") is not None:
        raise CoreError("任务类型与内容不匹配。", "task_kind_mismatch")
    if text is None:
        return
    if (not isinstance(text, dict) or text.get("source") != source or text.get("model") != recipe.get("model") or
            text.get("direction") != recipe.get("direction")):
        raise CoreError("原文、配置与任务身份不匹配。")
    task_id(text.get("id"))
    if text.get("phase") not in ("preparing", "running", "completed", "partial", "stopped", "failed"):
        raise CoreError("任务阶段无效。")
    attempts = text.get("attempts", {})
    if not isinstance(attempts, dict) or any(not k.isdigit() or type(v) is not int or v < 0 for k, v in attempts.items()):
        raise CoreError("重试计数无效。")
    offset = 0
    parts = text.get("segments")
    if not isinstance(parts, list) or len(parts) > 4096:
        raise CoreError("文字段计划无效。")
    binding = recipe_binding(recipe)
    for i, part in enumerate(parts):
        if not isinstance(part, dict): raise CoreError("文字段字段无效。")
        body = part.get("source")
        if (type(part.get("id")) is not int or part["id"] != i or not isinstance(body, str) or
                type(part.get("utf8Start")) is not int or type(part.get("utf8End")) is not int or
                part.get("utf8Start") != offset or part.get("utf8End") != offset + len(body.encode()) or
                part.get("state") not in STATES):
            raise CoreError("文字来源范围或状态无效。")
        offset += len(body.encode())
        if part["state"] == "completed":
            result = part.get("result", {})
            if (not isinstance(result, dict) or result.get("source") != trim(body) or result.get("model") != text["model"] or
                    result.get("direction") != text["direction"] or not isinstance(result.get("translation"), str) or not trim(result["translation"]) or
                    result.get("configurationBinding") not in (None, binding)):
                raise CoreError("旧成功结果与配置绑定不匹配。", "binding_mismatch")
            if (type(result.get("elapsedSeconds")) not in (int, float) or not math.isfinite(result["elapsedSeconds"]) or
                    not isinstance(result.get("warnings"), list) or any(not isinstance(v, str) for v in result["warnings"])):
                raise CoreError("完整结果字段无效。")
    if parts and "".join(p["source"] for p in parts) != source:
        raise CoreError("段计划未覆盖完整原文。")


def decode_snapshot(raw):
    if len(raw) > MAX_SNAPSHOT_BYTES * 2:
        raise CoreError("保存文件过大。")
    envelope = strict_json(raw)
    if not isinstance(envelope, dict): raise CoreError("保存文件不是对象。")
    if type(envelope.get("schema")) is not int or envelope["schema"] not in (1,2):
        raise CoreError("不支持此保存版本；原文件保留。", "future_schema")
    try:
        payload = base64.b64decode(envelope["payload"], validate=True)
    except (ValueError, KeyError, TypeError) as exc:
        raise CoreError("保存文件base64无效。") from exc
    if len(payload) > MAX_SNAPSHOT_BYTES or sha(payload) != envelope.get("checksum"):
        raise CoreError("任务checksum不匹配。", "snapshot_corrupt")
    task = strict_json(payload)
    if (envelope['schema']==2 and task.get('formatVersion')!=2 or
            envelope['schema']==1 and task.get('formatVersion') not in (None,1)):
        raise CoreError("保存版本与有效字段不匹配。", "future_schema")
    validate_task(task)
    return task


def encode_snapshot(task):
    validate_task(task)
    payload = foundation_json(task)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise CoreError("任务快照超出预览容量。")
    schema=task.get('formatVersion',1)
    if schema not in (1,2): raise CoreError("不支持此保存版本。", "future_schema")
    return foundation_json({"schema": schema, "checksum": sha(payload), "payload": base64.b64encode(payload).decode()})


def recover_in_memory(task):
    """Opening does not mutate the saved bytes, replan, or start inference."""
    task = deepcopy(task)
    text = task.get("text")
    if text and text.get("phase") in ("preparing", "running"):
        text["phase"] = "stopped"
    if text:
        for part in text["segments"]:
            if part["state"] == "processing":
                part["state"] = "stopped"
    document = task.get("document")
    if document:
        for page in document["pages"]:
            if page["state"] in ("pending", "recognizing"):
                page["state"] = "stopped"
        if any(p["state"] == "stopped" for p in document["pages"]):
            document["phase"] = "提取中断，等待主动继续"
    return task


def bilingual_text(task):
    if task.get("kind") == "文档":
        from .documents import document_text
        return document_text(task)
    recipe, source = task["configuration"], task["input"]
    text = task.get("text") or {"phase": "preparing", "segments": []}
    parts = text["segments"]
    output = f"文字翻译 · 双语对照\n方向：{recipe['direction']}\n模型：{recipe['model']}\n"
    output += f"状态：{text['phase']} · 共{len(parts)}段 · 已完成{sum(p['state']=='completed' for p in parts)}段\n"
    output += "本文件为导出时已确认状态的快照；未完成段不算成功译文。完成不代表准确率保证，请核对原文。\n"
    if text.get("error"):
        output += "任务提示：" + text["error"] + "\n"
    if not parts:
        return output + "\n[完整原文]\n" + source + "\n\n[译文]\n【尚未翻译】\n"
    for part in parts:
        output += f"\n===== 第{part['id']+1}段 · 原文 UTF-8 [{part['utf8Start']}, {part['utf8End']}) =====\n[原文]\n{part['source']}\n[译文]\n"
        if part["state"] == "completed":
            result = part["result"]; output += result["translation"] + "\n"
            for warning in result.get("warnings", []):
                output += "核对提示：" + warning + "\n"
        else:
            output += "【" + LABELS[part["state"]] + "】\n"
            if part.get("error"):
                output += "原因：" + part["error"] + "\n"
    return output
