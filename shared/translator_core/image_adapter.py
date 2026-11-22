"""Direct PNG/JPEG native facts, independent of the PDF adapter."""
import sys
from copy import deepcopy
from pathlib import Path
from . import CoreError, Cancelled
from .images import validate_geometry, FACT_CONTRACT, OCR_CONTRACT, PREVIEW_EDGE
from .ocr_adapter import MAX_OCR_FACT_BYTES
from .pdf_adapter import PDFAdapter
from .languages import vision_languages


class ImageAdapter(PDFAdapter):
    error_label='本地图片处理失败：'
    error_code='image_adapter_error'
    def __init__(self, root, command):
        super().__init__(root, command, probe='image_probe')
        self.languages = self.probe.get('availableLanguages')
        if (self.probe.get('contract') != FACT_CONTRACT or not isinstance(self.languages, list) or
                any(not isinstance(v, str) for v in self.languages)):
            raise CoreError('图片OCR事实能力不兼容。', 'image_adapter_mismatch')

    def profile(self, direction):
        return dict(contract=OCR_CONTRACT, identity=self.identity, native=self.probe['profile'],
            languages=vision_languages(direction),
            maxEdge=3000, maxPixels=9_000_000, upscale=False, alphaBackground='white')

    def require(self, direction):
        missing=set(self.profile(direction)['languages'])-set(self.languages)
        if missing: raise CoreError('本机缺少离线识别语言：'+', '.join(sorted(missing)), 'ocr_resource_missing')

    def metadata(self, source, fingerprint, cancel):
        result=self.call('image_metadata', source, fingerprint, cancel=cancel)
        validate_geometry(result.get('geometry'))
        return result['geometry']

    def preview(self, source, fingerprint, source_id, cancel):
        result=self.call('image_render', source, fingerprint, cancel=cancel,
            fields=dict(sourceID=source_id, maxEdge=PREVIEW_EDGE))
        validate_geometry(result.get('geometry'))
        if result.get('sourceID')!=source_id: raise CoreError('预览来源身份已改变。','image_source_mismatch')
        return result

    def recognize(self, source, fingerprint, unit, direction, cancel, source_id=None):
        self.require(direction)
        result=self.call('image_ocr', source, fingerprint, cancel=cancel,
            fields=dict(direction=direction, sourceID=source_id, recognitionLanguages=self.profile(direction)['languages']),
            limit=MAX_OCR_FACT_BYTES)
        if cancel.is_set(): raise Cancelled()
        raw=result.get('ocr')
        if (result.get('unit')!=unit or not isinstance(result.get('text'),str) or not isinstance(raw,dict) or
                raw.get('contract')!=OCR_CONTRACT or raw.get('backend')!=self.identity or
                raw.get('request')!=result['request'] or raw.get('sourceID')!=source_id or
                raw.get('sourceFingerprint')!=fingerprint or raw.get('recognitionComplete') is not True or
                raw.get('languages')!=self.profile(direction)['languages']):
            raise CoreError('图片OCR响应不属于当前请求/来源。','image_source_mismatch')
        raw['resourceProfile']=deepcopy(self.profile(direction));raw['elapsedSeconds']=result['elapsedSeconds']
        return result


def platform_image_adapter(root, helper=None):
    if sys.platform!='darwin' or not helper or not Path(helper).is_file():
        raise CoreError('本机图片事实适配器不可用；可回看已保存文字与导出。','image_adapter_missing')
    return ImageAdapter(root,[str(helper)])
