"""Direct loopback Ollama requests. No proxies, downloads, service control or unload."""
import http.client
import re
import socket
import threading
import time
from copy import deepcopy
from urllib.parse import urlsplit

from . import Cancelled, CoreError
from .contracts import recipe_binding, strict_json, validate_recipe, trim

OUTPUT_BYTES = 256_000


class RequestSocket(socket.socket):
    """Poll inside SocketIO, never expose a polling timeout to its buffer.

    Closing/shutting down another thread's socket does not reliably wake the
    Windows timeout select. Catch short transport waits below makefile(), so a
    cancelled header or buffered readline is bounded without corrupting that
    buffer or shortening the original 120/600-second request budgets.
    """
    def __init__(self, family, cancelled, deadline):
        super().__init__(family, socket.SOCK_STREAM)
        self.cancelled, self.deadline = cancelled, deadline

    def checkpoint(self, read_deadline):
        if self.cancelled.is_set(): raise Cancelled()
        if time.monotonic() >= min(self.deadline, read_deadline):
            raise socket.timeout("Owned request reached its bounded deadline")

    def recv_into(self, buffer, nbytes=0, flags=0):
        read_deadline = time.monotonic() + 120
        while True:
            self.checkpoint(read_deadline)
            try: return super().recv_into(buffer, nbytes, flags)
            except socket.timeout: pass

    def sendall(self, data, flags=0):
        write_deadline = time.monotonic() + 120
        remaining = memoryview(data).cast("B")
        while remaining:
            self.checkpoint(write_deadline)
            try:
                count = super().send(remaining, flags)
                if count == 0: raise ConnectionError("Owned request socket closed")
                remaining = remaining[count:]
            except socket.timeout: pass


def endpoint(value):
    try:
        url = urlsplit(value)
        port = url.port or 11434
    except (ValueError, TypeError) as exc:
        raise CoreError("服务地址无效。", "invalid_endpoint") from exc
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1") or
            url.username is not None or url.password is not None or
            url.path not in ("", "/") or url.query or url.fragment or not 0 < port <= 65535):
        raise CoreError("只接受本机回环HTTP服务地址。", "invalid_endpoint")
    return url.hostname, port


def content_warnings(source, target):
    warnings = []
    if any(s in source.casefold() for s in ("before", "after", "at least", "at most", "only if", "unless", "may not",
                                           "之前", "之后", "至少", "至多", "每天", "每次", "不得")):
        warnings.append("请逐项核对期限、否定、必要条件和频率；模型可能改变边界或义务，数字一致也不保证正确。")
    pattern = r"\d+(?:[.,]\d+)*(?:%)?"
    numbers = lambda s: sorted(n.replace(",", "") for n in re.findall(pattern, s))
    if numbers(source) != numbers(target):
        warnings.append("数字形式或数量有变化，请人工核对；这不代表已判定误译。")
    return warnings


class Ollama:
    def __init__(self, address="http://127.0.0.1:11434", cancel=None):
        self.host, self.port = endpoint(address)
        self.cancelled = cancel or threading.Event()
        self.lock = threading.Lock()
        self.sockets = set()

    def check(self):
        if self.cancelled.is_set(): raise Cancelled()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            for sock in tuple(self.sockets):
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                try: sock.close()
                except OSError: pass

    def _connect(self, connection, deadline):
        self.check()
        family = socket.AF_INET6 if self.host == "::1" else socket.AF_INET
        sock = RequestSocket(family, self.cancelled, deadline)
        sock.settimeout(5)
        with self.lock:
            self.sockets.add(sock)
            if self.cancelled.is_set(): sock.close(); raise Cancelled()
        try:
            sock.connect((self.host, self.port)); self.check()
            sock.settimeout(.1); connection.sock = sock
        except BaseException:
            with self.lock: self.sockets.discard(sock)
            sock.close(); raise

    def _request(self, path, body=None, stream=False):
        """One owned socket per request, including headers/readline cancellation."""
        import json
        self.check()
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        deadline = time.monotonic() + (600 if stream else 10)
        connection.connect = lambda: self._connect(connection, deadline)
        initial_sockets = set()
        response = None
        try:
            raw = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            connection.request("GET" if body is None else "POST", path, body=raw,
                               headers={} if raw is None else {"Content-Type": "application/json"})
            with self.lock: initial_sockets = set(self.sockets)
            response = connection.getresponse(); self.check()
            if response.status != 200:
                raise CoreError(f"本地服务HTTP {response.status}；没有云端回退或自动下载。", "service_unavailable")
            if stream:
                yield response
            else:
                data = response.read(2 * 1024 * 1024 + 1); self.check()
                if len(data) > 2 * 1024 * 1024:
                    raise CoreError("服务诊断响应过大。")
                yield strict_json(data)
        except (OSError, http.client.HTTPException) as exc:
            self.check()
            raise CoreError("本地服务连接中断或超时，请检查服务后主动继续。", "service_unavailable") from exc
        finally:
            if response is not None: response.close()
            connection.close()
            with self.lock:
                # An engine is serial; keep references even when HTTPConnection
                # clears .sock because the server sent Connection: close.
                for sock in tuple(self.sockets | initial_sockets):
                    sock.close(); self.sockets.discard(sock)

    def json(self, path, body=None):
        generator = self._request(path, body)
        try: return next(generator)
        finally: generator.close()

    def verify(self, recipe):
        validate_recipe(recipe)
        version = self.json("/api/version")
        if not isinstance(version, dict) or version.get("version") != "0.35.0":
            raise CoreError("此固定组合需要本机Ollama 0.35.0，请保留原服务并核对。", "service_incompatible")
        tags = self.json("/api/tags")
        if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
            raise CoreError("本地模型清单无效。", "service_incompatible")
        selected = next((m for m in tags["models"] if isinstance(m, dict) and m.get("name") == recipe["model"]), None)
        if not selected or selected.get("digest") != recipe["modelDigest"]:
            raise CoreError("模型缺失或digest不匹配；恢复原资源或另建任务，不自动下载。", "model_mismatch")
        if "cloud" in recipe["model"].lower() or any(selected.get(k) is not None for k in ("remote_host", "remote_model")):
            raise CoreError("拒绝云模型。", "cloud_rejected")
        show = self.json("/api/show", {"model": recipe["model"]})
        if (not isinstance(show, dict) or any(show.get(k) is not None for k in ("remote_host", "remote_model")) or
                not isinstance(show.get("capabilities"), list) or "completion" not in show["capabilities"]):
            raise CoreError("无法确认模型为支持本地文字生成的模型。", "model_incompatible")
        return {"version": version["version"], "model": recipe["model"], "digest": selected["digest"],
                "capabilities": show["capabilities"], "inference": False}

    def translate(self, source, recipe):
        self.check(); validate_recipe(recipe)
        prompt_bytes = len(recipe["userPrefix"].encode()) + len(recipe.get("systemPrompt", "").encode())
        if not trim(source) or len(source.encode()) > 2048 or prompt_bytes + 64 > 1024:
            raise CoreError("单次请求超出原有保守容量预算。", "budget_exceeded")
        info = self.verify(recipe)
        messages = [{"role": "user", "content": recipe["userPrefix"] + source}]
        if recipe.get("systemPrompt"):
            messages.insert(0, {"role": "system", "content": recipe["systemPrompt"]})
        body = {"model": recipe["model"], "stream": True, "keep_alive": recipe["keepAlive"],
                "messages": messages, "options": deepcopy(recipe["options"])}
        if "thinking" in info["capabilities"] and recipe.get("disableThinking"):
            body["think"] = False
        start = time.monotonic(); first = None; pieces = []; count = 0; final = None
        generator = self._request("/api/chat", body, stream=True)
        try:
            response = next(generator)
            while True:
                self.check()
                line = response.readline(OUTPUT_BYTES + 1); self.check()
                if not line: break
                if len(line) > OUTPUT_BYTES:
                    raise CoreError("模型响应行过大。", "incomplete", False)
                if not line.strip(): continue
                chunk = strict_json(line)
                if not isinstance(chunk, dict): raise CoreError("模型响应无效。", "incomplete", False)
                if chunk.get("error"): raise CoreError("Ollama：" + str(chunk["error"]), "service_unavailable")
                message = chunk.get("message") or {}
                delta = message.get("content", "") if isinstance(message, dict) else None
                if not isinstance(delta, str): raise CoreError("模型响应无效。", "incomplete", False)
                count += len(delta.encode())
                if count > OUTPUT_BYTES: raise CoreError("模型输出超过原有验证上限。", "incomplete", False)
                if delta and first is None: first = time.monotonic() - start
                pieces.append(delta)
                if chunk.get("done") is True:
                    if chunk.get("done_reason") != "stop":
                        raise CoreError("生成未正常结束；不保存为成功。", "incomplete", False)
                    final = chunk; break
            self.check()
            translation = "".join(pieces)
            if final is None or not trim(translation):
                raise CoreError("响应中断或译文为空；未确认成功。", "incomplete", False)
        finally: generator.close()
        result = dict(source=source, translation=translation, model=recipe["model"], direction=recipe["direction"],
                      elapsedSeconds=time.monotonic() - start, warnings=content_warnings(source, translation),
                      configurationBinding=recipe_binding(recipe), requestConfiguration=deepcopy(recipe),
                      promptProfile=recipe["promptProfile"])
        if first is not None: result["firstTextSeconds"] = first
        for key, destination, factor in (("eval_count", "outputTokens", 1), ("load_duration", "modelLoadSeconds", 1e9),
                ("prompt_eval_count", "promptTokens", 1), ("prompt_eval_duration", "promptEvaluationSeconds", 1e9),
                ("eval_duration", "generationSeconds", 1e9)):
            if type(final.get(key)) in (int, float): result[destination] = final[key] / factor if factor != 1 else final[key]
        return result
