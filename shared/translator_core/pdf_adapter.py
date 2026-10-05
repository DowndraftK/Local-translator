"""Bounded file handoff to disposable native fact processes, with checked identity."""
import json
import math
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from . import Cancelled, CoreError
from .contracts import MAX_INPUT_BYTES, sha, strict_json
from .store import atomic_write, safe_file, file_identity, is_link

MAX_IMAGE_BYTES = 40_000_000  # one 9M-pixel RGBA image plus bounded PNG overhead


class PDFAdapter:
    def __init__(self, root, command):
        self.root, self.command = Path(root).absolute(), list(map(str, command))
        if is_link(self.root): raise CoreError("页暂存目录不安全。", "unsafe_path")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.identity = None; self.verified = False
        result = self.call("probe", cancel=threading.Event())
        self.identity = result["backend"]; self.verified = True

    def call(self, action, source=None, fingerprint=None, page=None, cancel=None):
        cancel = cancel or threading.Event()
        if cancel.is_set(): raise Cancelled()
        if source is not None and file_identity(source, cancel=cancel)[1] != fingerprint:
            raise CoreError("来源副本hash不匹配，不从原路径替换。", "source_mismatch")
        folder = self.root / uuid.uuid4().hex; folder.mkdir(mode=0o700)
        request_id = str(uuid.uuid4())
        request = dict(schema=1, request=request_id, action=action)
        if source is not None: request.update(source=str(source), fingerprint=fingerprint)
        if page is not None: request["page"] = page
        if action == "render": request["image"] = str(folder / "page.png")
        request_path, result_path = folder / "request.json", folder / "result.json"
        atomic_write(json.dumps(request, ensure_ascii=False).encode(), request_path)
        process = None
        keep_image = False
        try:
            with (folder / "stderr").open("wb") as error:
                process = subprocess.Popen(self.command + [str(request_path), str(result_path)],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error)
                deadline = time.monotonic() + 60
                while process.poll() is None:
                    if cancel.wait(.03):
                        process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired: process.kill(); process.wait()
                        raise Cancelled()
                    if time.monotonic() > deadline:
                        process.kill(); process.wait()
                        raise CoreError("当前页事实处理超过60秒，已停止此自有适配进程。", "adapter_timeout")
            if cancel.is_set(): raise Cancelled()
            if process.returncode:
                error = safe_file(folder / "stderr", 8192).decode("utf-8", "replace")
                raise CoreError("本地PDF处理失败：" + error, "pdf_adapter_error", False)
            raw = safe_file(result_path, MAX_INPUT_BYTES + 65536)
            result = strict_json(raw)
            if (not isinstance(result, dict) or result.get("schema") != 1 or result.get("request") != request_id or
                    result.get("action") != action or result.get("complete") is not True or not isinstance(result.get("backend"), str) or
                    self.identity is not None and result["backend"] != self.identity or
                    source is not None and result.get("fingerprint") != fingerprint or
                    page is not None and result.get("page") != page):
                raise CoreError("事实文件响应/来源/页/后端身份不匹配。", "adapter_identity_mismatch")
            if action == "metadata" and (type(result.get("totalPages")) is not int or result["totalPages"] < 1):
                raise CoreError("PDF没有有效物理页。")
            if action == "render":
                image = result.get("image", {}); width, height = image.get("pixelWidth"), image.get("pixelHeight")
                if (type(width) is not int or type(height) is not int or not 0 < width <= 3000 or not 0 < height <= 3000 or
                        width * height > 9_000_000 or image.get("rotation") not in (0, 90, 180, 270) or
                        type(image.get("scale")) not in (int, float) or not math.isfinite(image["scale"]) or not 0 < image["scale"] <= 3):
                    raise CoreError("渲染结果超出固定像素/方向边界。", "resource_limit")
                image_path = folder / "page.png"
                count, image_sha = file_identity(image_path, MAX_IMAGE_BYTES)
                if (count, image_sha) != (image.get("byteCount"), image.get("sha256")):
                    raise CoreError("渲染位图大小/hash不匹配。", "image_identity_mismatch")
                # The UI consumes this one file, validates again, and acknowledges deletion.
                result["image_file"] = str(image_path.relative_to(self.root))
                keep_image = True
            return result
        finally:
            if process and process.poll() is None: process.kill(); process.wait()
            for name in ("request.json", "result.json", "stderr"): (folder / name).unlink(missing_ok=True)
            if not keep_image: shutil.rmtree(folder)

    def metadata(self, source, fingerprint, cancel): return self.call("metadata", source, fingerprint, cancel=cancel)
    def page(self, source, fingerprint, page, cancel): return self.call("page", source, fingerprint, page, cancel)
    def render(self, source, fingerprint, page, cancel): return self.call("render", source, fingerprint, page, cancel)


def platform_pdf_adapter(root, helper=None, python=None, library=None):
    if sys.platform == "darwin":
        if not helper or not Path(helper).is_file(): raise CoreError("Mac PDFKit事实适配器缺失。", "pdf_adapter_missing")
        command = [str(helper)]
    elif sys.platform == "win32":
        script = Path(__file__).resolve().parents[1] / "platform/windows/pdf_facts.py"
        if not python or not library or not Path(library).is_dir():
            raise CoreError("固定Windows PDFium软件库缺失；不会自动补包。", "pdf_adapter_missing")
        command = [str(python), "-I", "-B", "-X", "utf8", str(script), "--library", str(library)]
    else: raise CoreError("PDF适配只在原生Mac/Windows执行。")
    return PDFAdapter(root, command)
