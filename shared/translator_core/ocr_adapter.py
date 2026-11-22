"""Bounded OCR fact bridge, reusing the B03 source/render handoff and cancellation."""
import base64
import sys
from copy import deepcopy
from pathlib import Path
from . import CoreError, Cancelled
from .pdf_adapter import PDFAdapter
from .store import is_link
from .languages import vision_languages

MAX_OCR_FACT_BYTES = 4 * 1024 * 1024


class OCRAdapter(PDFAdapter):
    def __init__(self, root, command, pdf):
        self.pdf = pdf
        super().__init__(root, command, probe='ocr_probe')
        languages = self.probe.get('availableLanguages')
        if not isinstance(languages, list) or any(not isinstance(v, str) for v in languages):
            raise CoreError('OCR实际启动语言清单无效。', 'ocr_adapter_mismatch')
        self.languages = languages

    def profile(self, direction):
        if sys.platform == 'win32':
            if direction not in ('en-zh', 'zh-en'): raise CoreError('OCR方向无效。')
            required = ['zh-Hans-CN'] if direction == 'zh-en' else ['en-US']
        else:
            required = vision_languages(direction)
        return dict(identity=self.identity, native=self.probe['profile'], languages=required,
                    pdfBackend=self.pdf.identity, maxEdge=3000, maxPixels=9_000_000, maxScale=3)

    def require(self, direction):
        missing = [v for v in self.profile(direction)['languages'] if v not in self.languages]
        if missing:
            raise CoreError('缺少离线OCR资源：'+', '.join(missing)+'。本批不自动下载或替换语言；可读已有结果。', 'ocr_resource_missing')

    def recognize(self, source, fingerprint, page, direction, cancel):
        self.require(direction)
        fields = dict(direction=direction)
        if sys.platform == 'darwin':
            fields['recognitionLanguages'] = self.profile(direction)['languages']
        image = None
        try:
            if sys.platform == 'win32':
                render = self.pdf.render(source, fingerprint, page, cancel)
                image = self.pdf.root / render['image_file']
                fields.update(image=str(image), imageBytes=render['image']['byteCount'], imageSHA256=render['image']['sha256'], render=render['image'])
            result = self.call('ocr', source, fingerprint, page, cancel, fields=fields, limit=MAX_OCR_FACT_BYTES)
            if cancel.is_set(): raise Cancelled()
            if not isinstance(result.get('text'), str) or not isinstance(result.get('ocr'), dict):
                raise CoreError('OCR响应不完整。', 'incomplete_ocr')
            raw = result['ocr']
            if (raw.get('backend') != self.identity or raw.get('request') != result['request'] or
                    raw.get('languages') != self.profile(direction)['languages'] or raw.get('recognitionComplete') is not True):
                raise CoreError('OCR响应引擎/请求/语言身份不匹配。', 'ocr_adapter_mismatch')
            raw['resourceProfile'] = deepcopy(self.profile(direction))
            raw['elapsedSeconds'] = result['elapsedSeconds']
            if image is not None:
                raw['renderElapsedSeconds'] = render['elapsedSeconds']
                if (raw['pixelWidth'],raw['pixelHeight']) != (render['image']['pixelWidth'],render['image']['pixelHeight']):
                    raise CoreError('OCR图像几何与已核验来源不匹配。', 'image_identity_mismatch')
            return result
        finally:
            if image is not None and not is_link(image):
                image.unlink(missing_ok=True); image.parent.rmdir()


def platform_ocr_adapter(root, pdf, helper=None):
    if pdf is None: raise CoreError('OCR需要本地PDF渲染适配器。', 'ocr_adapter_missing')
    if sys.platform == 'darwin':
        if not helper or not Path(helper).is_file(): raise CoreError('Mac Vision事实适配器缺失。', 'ocr_adapter_missing')
        command = [str(helper)]
    elif sys.platform == 'win32':
        script = Path(__file__).resolve().parents[1] / 'platform/windows/ocr_facts.ps1'
        # Only this packaged, auditable bridge source is read. No policy or global
        # configuration is changed; the fixed source runs in its disposable process.
        return WindowsOCRAdapter(root, [], pdf, script)
    else: raise CoreError('OCR只在原生Mac/Windows运行。', 'ocr_adapter_missing')
    return OCRAdapter(root, command, pdf)


class WindowsOCRAdapter(OCRAdapter):
    def __init__(self, root, command, pdf, script):
        self.script = script
        super().__init__(root, command, pdf)

    def command_for(self, request_path, result_path):
        def quote(path): return "'" + str(path).replace("'", "''") + "'"
        source = "$bridge=[ScriptBlock]::Create([IO.File]::ReadAllText(" + quote(self.script) + ")); & $bridge " + quote(request_path) + " " + quote(result_path)
        return ['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(source.encode('utf-16le')).decode()]
