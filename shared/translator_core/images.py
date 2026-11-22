"""Image-only source/body contracts; no PDF fields or invented physical pages."""
from . import CoreError
from .contracts import task_id

SOURCE_CONTRACT = 'image-source-v1'
FACT_CONTRACT = 'image-facts-v1'
OCR_CONTRACT = 'image-ocr-v1'
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_PIXELS = 40_000_000
PREVIEW_EDGE = 2400


def content(task):
    return task['image'] if task.get('kind') == '图片' else task['document']


def units(manifest):
    return manifest['units'] if manifest.get('mediaKind') == 'image' else manifest['pages']


def unit_field(manifest):
    return 'unit' if manifest.get('mediaKind') == 'image' else 'page'


def validate_geometry(value):
    if not isinstance(value, dict) or value.get('contract') != FACT_CONTRACT:
        raise CoreError('图片事实版本不兼容。', 'image_contract_mismatch')
    w, h = value.get('sourceWidth'), value.get('sourceHeight')
    orientation = value.get('exifOrientation')
    if (type(w) is not int or type(h) is not int or min(w, h) < 1 or w*h > MAX_SOURCE_PIXELS or
            type(orientation) is not int or orientation not in range(1, 9) or
            value.get('format') not in ('PNG', 'JPEG') or type(value.get('hasAlpha')) is not bool or
            value.get('frameCount') != 1):
        raise CoreError('图片格式、方向或像素超出保护。', 'image_geometry')
    display = (h, w) if orientation >= 5 else (w, h)
    if (value.get('displayWidth'), value.get('displayHeight')) != display:
        raise CoreError('应用方向后的显示尺寸不匹配。', 'image_geometry')


def validate_source(source):
    if not isinstance(source, dict) or source.get('contract') != SOURCE_CONTRACT:
        raise CoreError('图片来源版本不兼容。', 'image_contract_mismatch')
    task_id(source.get('id'))
    fingerprint = source.get('fingerprint')
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64 or
            any(c not in '0123456789abcdef' for c in fingerprint) or
            type(source.get('byteCount')) is not int or not 0 < source['byteCount'] <= MAX_SOURCE_BYTES or
            source.get('origin') not in ('file-original', 'clipboard-encoded', 'clipboard-pixels-png') or
            not isinstance(source.get('file'), str) or not source['file'] or len(source['file'].encode()) > 4096):
        raise CoreError('图片来源身份无效。', 'image_source_mismatch')
    if source['origin'] == 'file-original':
        if not isinstance(source.get('originalPath'), str) or not source['originalPath']:
            raise CoreError('文件来源记录缺失。', 'image_source_mismatch')
    elif source.get('originalPath') is not None:
        raise CoreError('剪贴板不能声称取得原文件路径。', 'image_source_mismatch')
    validate_geometry(source.get('geometry'))
    if source['origin'] == 'clipboard-pixels-png' and source['geometry']['format'] != 'PNG':
        raise CoreError('剪贴板像素规范来源必须为无损PNG。', 'image_source_mismatch')


def validate_image_task(task):
    from .documents import validate_content
    from .ocr_contracts import validate_ocr
    image = task.get('image'); recipe = task['configuration']
    if task.get('formatVersion') != 3 or task.get('document') is not None or task['input'] != '':
        raise CoreError('图片任务须使用独立schema3，不能伪装文档。', 'task_kind_mismatch')
    if (not isinstance(image, dict) or image.get('mediaKind') != 'image' or
            image.get('id') != task['id'] or image.get('model') != recipe['model'] or
            image.get('direction') != recipe['direction'] or image.get('mode') != 'ocr' or
            type(image.get('translationStarted')) is not bool or not isinstance(image.get('phase'), str) or
            any(k in image for k in ('isPDF', 'totalPages', 'range', 'pages', 'cropBox', 'label'))):
        raise CoreError('独立图片清单身份无效。', 'image_source_mismatch')
    validate_source(image.get('source'))
    source = image['source']; asset = task.get('asset')
    if (not isinstance(asset, dict) or asset.get('fingerprint') != source['fingerprint'] or
            asset.get('byteCount') != source['byteCount'] or image.get('fingerprint') != source['fingerprint'] or
            image.get('file') != source['file']):
        raise CoreError('图片封存来源与清单不匹配。', 'image_source_mismatch')
    slots = image.get('units')
    if not isinstance(slots, list) or len(slots) != 1 or not isinstance(slots[0], dict) or type(slots[0].get('id')) is not int or slots[0]['id'] != 1:
        raise CoreError('图片只支持一个逻辑内容单元。', 'image_contract_mismatch')
    if any(k in slots[0] for k in ('label', 'cropBox')):
        raise CoreError('图片内容不能带PDF印刷页或裁切字段。', 'image_contract_mismatch')
    validate_content(task, image, slots)
    if any('page' in mapping for mapping in image['sources']):
        raise CoreError('图片段映射使用逻辑单元，不能携带PDF页码。','image_contract_mismatch')
    validate_ocr(task, {1: slots[0]}, image=True)


def image_text(task, only_page=None):
    from .documents import content_text
    image = task['image']; source = image['source']; geometry = source['geometry']
    origins = {'file-original': '文件原字节', 'clipboard-encoded': '剪贴板编码表示',
               'clipboard-pixels-png': '剪贴板像素规范编码（无损PNG；原文件格式、路径和EXIF不可得）'}
    header = ['图片双语对照', f"图片：{source['file']}", f"来源UUID：{source['id']}",
        f"来源 SHA-256：{source['fingerprint']} · {source['byteCount']}字节", '来源方式：'+origins[source['origin']],
        '原路径记录：'+(source.get('originalPath') or '不可得（剪贴板）'),
        f"实际格式：{geometry['format']} · 原始像素 {geometry['sourceWidth']}×{geometry['sourceHeight']}",
        f"EXIF方向：{geometry['exifOrientation']} · 显示像素 {geometry['displayWidth']}×{geometry['displayHeight']}",
        '透明背景在预览与OCR中合成白色；展示缩放不改变源字节。不自动纠偏。',
        f"模型：{image['model']} · 方向：{image['direction']} · 图片状态：{image['phase']}",
        '原始识别、有效原文、人工核对、翻译状态分别记录。未应用草稿不属于有效原文。']
    return '\n'.join(header + content_text(task, image, only_page))+'\n'
