"""Shared document manifest, page-local sources and schema1 compatibility.

Adapters provide facts only. This file owns ranges, validation and complete TXT.
"""
import math
from . import CoreError
from .contracts import MAX_INPUT_BYTES, STATES, LABELS, recipe_binding, task_id, trim
from .planning import split

PAGE_STATES = {"pending", "recognizing", "extracted", "noText", "failed", "stopped"}
PAGE_LABELS = {"pending": "待提取", "recognizing": "当前页处理中", "extracted": "有文字", "noText": "无文字",
               "failed": "失败", "stopped": "停止未处理"}
LIMITATIONS = "只提取文字层；无文字页须由用户主动另建OCR任务。保留页内空白、重复、页眉页脚；不跨页拼句。复杂版面及文字顺序须人工核对。"


def page_range(first, last, total):
    if any(type(n) is not int for n in (first, last, total)) or not 1 <= first <= last <= total or last - first >= 200:
        raise CoreError("请选择文件内连续物理页码，每任务最多200页；可选择201–220，反向/越界/超量不裁剪。", "invalid_page_range")
    return dict(first=first, last=last)


def source_key(document):
    return document["id"] + "".join(page["revision"] for page in document["pages"])


def validate_document_task(task):
    d, recipe, text = task.get("document"), task["configuration"], task.get("text")
    if not isinstance(d, dict): raise CoreError("缺少文档清单。")
    if (d.get("id") != task["id"] or d.get("model") != recipe["model"] or d.get("direction") != recipe["direction"] or
            type(d.get("isPDF")) is not bool or type(d.get("translationStarted")) is not bool or
            not isinstance(d.get("file"), str) or not isinstance(d.get("phase"), str) or d.get("mode") not in ("text", "ocr")):
        raise CoreError("文档身份、阶段或配置无效。", "document_identity_mismatch")
    r = d.get("range", {}); page_range(r.get("first"), r.get("last"), d.get("totalPages"))
    if not d["isPDF"] and (d["totalPages"] != 1 or r != {"first": 1, "last": 1} or d["mode"] != "text"):
        raise CoreError("TXT只能为全文，不能伪造PDF页码。")
    asset = task.get("asset")
    if (not isinstance(asset, dict) or asset.get("fingerprint") != d.get("fingerprint") or
            type(asset.get("byteCount")) is not int or not 0 < asset["byteCount"] <= 256_000_000):
        raise CoreError("文档封存来源身份无效。")
    pages = d.get("pages")
    if not isinstance(pages, list) or [p.get("id") for p in pages if isinstance(p, dict)] != list(range(r["first"], r["last"] + 1)):
        raise CoreError("每个所选物理页都必须保留在清单中。")
    by_id = {}
    for p in pages:
        if (p.get("state") not in PAGE_STATES or p.get("mode") not in ("text", "ocr") or
                not isinstance(p.get("text"), str) or type(p.get("edited")) is not bool or type(p.get("reviewed")) is not bool):
            raise CoreError("页状态、文字或核对字段无效。")
        task_id(p.get("revision"))
        if p.get("label") is not None and not isinstance(p["label"], str): raise CoreError("印刷标签无效。")
        by_id[p["id"]] = p
    if sum(len(p["text"].encode()) for p in pages) > MAX_INPUT_BYTES:
        raise CoreError("所选页累计文字超出1MiB UTF-8保护。", "resource_limit")
    if any(not str(n).isdigit() or int(n) not in by_id or not isinstance(body, str) for n, body in task["drafts"].items()):
        raise CoreError("草稿来源页不匹配。", "draft_revision_mismatch")
    plan, sources = d.get("segments"), d.get("sources")
    if not isinstance(plan, list) or not isinstance(sources, list) or len(plan) != len(sources) or len(plan) > 4096:
        raise CoreError("文档计划或来源映射无效。", "resource_limit")
    cursor = {p["id"]: 0 for p in pages}
    for i, (part, m) in enumerate(zip(plan, sources)):
        if not isinstance(part, dict) or not isinstance(m, dict): raise CoreError("文档段字段无效。")
        p = by_id.get(m.get("page"))
        if (p is None or m.get("segmentID") != i or m.get("revision") != p["revision"] or
                type(m.get("paragraph")) is not int or m["paragraph"] < 1 or part.get("id") != i or
                type(m.get("utf8Start")) is not int or type(m.get("utf8End")) is not int or
                m["utf8Start"] != cursor[p["id"]] or not m["utf8Start"] <= m["utf8End"] <= len(p["text"].encode()) or
                (part.get("utf8Start"), part.get("utf8End")) != (m["utf8Start"], m["utf8End"]) or
                part.get("state") not in STATES or not isinstance(part.get("source"), str) or
                p["text"].encode()[m["utf8Start"]:m["utf8End"]] != part["source"].encode()):
            raise CoreError("文档段不属于有效页修订或来源范围。", "source_revision_mismatch")
        cursor[p["id"]] = m["utf8End"]
    if plan and any(trim(p["text"]) and cursor[p["id"]] != len(p["text"].encode()) for p in pages):
        raise CoreError("已有计划未覆盖全部有效原文。", "source_revision_mismatch")
    if text is None: return
    if (not isinstance(text, dict) or text.get("source") != source_key(d) or text.get("model") != recipe["model"] or
            text.get("direction") != recipe["direction"] or text.get("phase") not in ("preparing", "running", "completed", "partial", "stopped", "failed")):
        raise CoreError("译文与来源修订身份不匹配。", "source_revision_mismatch")
    task_id(text.get("id"))
    parts = text.get("segments")
    if not isinstance(parts, list) or len(parts) != len(plan): raise CoreError("已有译文计划数量不匹配。")
    attempts = text.get("attempts", {})
    if not isinstance(attempts, dict) or any(not k.isdigit() or type(v) is not int or v < 0 for k, v in attempts.items()):
        raise CoreError("重试计数无效。")
    for part, original in zip(parts, plan):
        if any(part.get(k) != original[k] for k in ("id", "source", "utf8Start", "utf8End")) or part.get("state") not in STATES:
            raise CoreError("译文已有计划与来源计划不匹配。", "source_revision_mismatch")
        if part["state"] == "completed":
            result = part.get("result", {})
            if (not isinstance(result, dict) or result.get("source") != trim(part["source"]) or
                    result.get("model") != recipe["model"] or result.get("direction") != recipe["direction"] or
                    not isinstance(result.get("translation"), str) or not trim(result["translation"]) or
                    result.get("configurationBinding") not in (None, recipe_binding(recipe)) or
                    type(result.get("elapsedSeconds")) not in (int, float) or not math.isfinite(result["elapsedSeconds"]) or
                    not isinstance(result.get("warnings"), list) or any(not isinstance(s, str) for s in result["warnings"])):
                raise CoreError("旧成功对象与原文/配置绑定不匹配。", "binding_mismatch")


def prepare_document(document, boundaries, cancel):
    # A saved plan is authoritative. Never invoke another splitter on it.
    if document["segments"]: return document["segments"]
    plan, sources = [], []
    for page in document["pages"]:
        if not trim(page["text"]): continue
        pieces = split(page["text"], boundaries, cancel)
        for paragraph, piece in enumerate(pieces, 1):
            if len(plan) >= 4096: raise CoreError("文档超过4096段保护。", "resource_limit")
            n = len(plan)
            sources.append(dict(segmentID=n, page=page["id"], paragraph=paragraph,
                                utf8Start=piece["utf8Start"], utf8End=piece["utf8End"], revision=page["revision"]))
            plan.append(dict(piece, id=n))
    if not plan: raise CoreError("所选范围没有可翻译文字；无文字页仍保留，请核对原页。", "zero_translatable_text")
    document["sources"], document["segments"] = sources, plan
    return plan


def coverage(document):
    pages = document["pages"]
    return ("识别进度" if document["mode"] == "ocr" else "提取覆盖") + "：" + " · ".join(
        f"{PAGE_LABELS[state]} {sum(p['state'] == state for p in pages)}" for state in ("pending", "recognizing", "extracted", "noText", "failed", "stopped"))


def page_source_label(page):
    if page["mode"] != "ocr": return "文字层"
    if not page["edited"]: return "原始OCR"
    if not trim(page["text"]): return "人工清空（不算OCR成功）"
    raw = page.get("ocr") or {}
    return "人工校正" if raw.get("observations") else "人工输入（不算OCR成功）"


def document_text(task, only_page=None):
    d = task["document"]; r = d["range"]; job = task.get("text") or {}; parts = job.get("segments", [])
    out = ["文档双语对照", f"文件：{d['file']}", f"来源 SHA-256：{d['fingerprint']}",
           f"来源记录：{d.get('originalPath', '')}", f"总物理页数：{d['totalPages'] if d['isPDF'] else '不适用（TXT全文）'}",
           f"选定范围：{str(r['first'])+'–'+str(r['last']) if d['isPDF'] else 'TXT全文'}", f"模式：{d['mode']}",
           f"提取后端：{d.get('extractionProfile', '身份缺失')}", f"模型：{d['model']} · 方向：{d['direction']}",
           f"任务状态：{d['phase']}", coverage(d),
           f"翻译进度：{sum(p['state']=='completed' for p in parts)} / {len(parts)}段 · {job.get('phase', '尚未开始')}",
           LIMITATIONS if d["mode"] == "text" else "整页显式OCR；不混文字层。置信度、识别成功与内容准确完整是不同记录。请核对数字、否定、重复与顺序。",
           "所有所选页与已获得原文均保留；未完成译文明确标记。未应用草稿不属于有效原文/翻译输入。"]
    for issue in (d.get("issue"), job.get("error")):
        if issue: out.append("提示：" + issue)
    for page in d["pages"]:
        if only_page is not None and page["id"] != only_page: continue
        out.extend(["", f"===== {'PDF物理页 '+str(page['id']) if d['isPDF'] else 'TXT全文'} · {PAGE_LABELS[page['state']]} =====",
                    f"有效原文修订：{page['revision']} · 来源：{page_source_label(page)}",
                    "人工核对：" + ("已核对当前修订" if page["reviewed"] else "未人工核对")])
        if page.get("label") is not None: out.append("可选印刷标签：" + page["label"])
        if page.get("issue"): out.append("页提示：" + page["issue"])
        raw = page.get("ocr")
        if raw:
            out.extend([f"渲染：{raw['pixelWidth']}×{raw['pixelHeight']} · rotation={raw['rotation']} · cropBox={raw['cropBox']}",
                        "坐标：" + raw["coordinateSystem"], "[原始OCR观察项：顺序/ID/置信度/坐标/文字]"])
            for o in raw["observations"]: out.append(f"{o['order']} / {o['id']} / {o['confidence']} / {o['rect']} / {o['text']}")
        out.extend(["[全部已获得的有效原文]", page["text"], "[对应译文]"])
        mapping = [m for m in d["sources"] if m["page"] == page["id"]]
        if not mapping: out.append("【尚未建立翻译计划】" if trim(page["text"]) else "【未获得可翻译原文；" + PAGE_LABELS[page["state"]] + "】")
        for m in mapping:
            part = parts[m["segmentID"]] if parts else None
            out.append(f"第{m['paragraph']}段 · 修订{m['revision']} · 页内UTF-8 [{m['utf8Start']}, {m['utf8End']})")
            if part and part["state"] == "completed":
                out.append(part["result"]["translation"])
                out.extend("核对提示：" + w for w in part["result"]["warnings"])
            else:
                out.append("【" + LABELS[part["state"] if part else "pending"] + "】")
                if part and part.get("error"): out.append("原因：" + part["error"])
        if str(page["id"]) in task["drafts"]: out.append("另有未应用编辑草稿；正式正文与翻译输入未使用此草稿。")
    return "\n".join(out) + "\n"
