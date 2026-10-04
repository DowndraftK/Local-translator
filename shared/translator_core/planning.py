"""Shared UTF-8 packing, using a small platform boundary adapter."""
import bisect
import json
import subprocess
import sys
import time
from pathlib import Path

from . import Cancelled, CoreError
from .contracts import SOURCE_BYTES, MAX_INPUT_BYTES


class MacBoundaries:
    def __init__(self, executable):
        self.executable = Path(executable)

    def __call__(self, source, cancel):
        if sys.platform != "darwin" or not self.executable.is_file():
            raise CoreError("此平台的旧分句适配器尚未验证，保留任务和原文。", "boundary_adapter_unavailable")
        if cancel.is_set(): raise Cancelled()
        process = subprocess.Popen([str(self.executable)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 20
        data = source.encode()
        while True:
            if cancel.is_set():
                process.kill(); process.communicate(); raise Cancelled()
            try:
                raw, error = process.communicate(data, timeout=.1); break
            except subprocess.TimeoutExpired as exc:
                data = None
                if time.monotonic() >= deadline:
                    process.kill(); process.communicate()
                    raise CoreError("分句准备超时，原文仍可导出。") from exc
        if cancel.is_set(): raise Cancelled()
        if process.returncode != 0:
            raise CoreError("Mac分句适配器未正常完成。")
        try: reply = json.loads(raw)
        except ValueError as exc: raise CoreError("分句适配器响应无效。") from exc
        if reply.get("schema") != 1 or reply.get("byteCount") != len(source.encode()):
            raise CoreError("分句适配器身份不匹配。")
        return reply["boundaries"]


def split(source, boundaries, cancel, byte_limit=SOURCE_BYTES):
    data = source.encode()
    from .contracts import trim
    if len(data) > MAX_INPUT_BYTES or not trim(source):
        raise CoreError("请输入非空文字，预览上限为1MiB UTF-8。")
    if type(byte_limit) is not int or byte_limit <= 0:
        raise CoreError("分段容量必须大于零。")
    rows = boundaries(source, cancel)
    ends, previous = [], 0
    for row in rows:
        if (not isinstance(row, list) or len(row) != 4 or any(type(v) is not int for v in row) or
                not previous < row[0] <= len(data) or any(v not in (0, 1) for v in row[1:])):
            raise CoreError("分句/字符边界无效。")
        try: data[previous:row[0]].decode()
        except UnicodeError as exc: raise CoreError("字符边界破坏UTF-8。") from exc
        previous = row[0]; ends.append(previous)
    if previous != len(data): raise CoreError("字符边界未覆盖完整原文。")
    pieces, whitespace, start_index, start = [], "", 0, 0
    while start < len(data):
        if cancel.is_set(): raise Cancelled()
        count = bisect.bisect_right(ends, start + byte_limit)
        if count <= start_index:
            raise CoreError("单个Unicode字符超出模型容量，原文仍可导出。")
        paragraph = sentence = word = None
        for end, newline, space, sent in rows[start_index:count]:
            if newline: paragraph = end
            if sent: sentence = end
            if space: word = end
        cursor = ends[count - 1]
        end = cursor if cursor == len(data) else (paragraph or sentence or word or cursor)
        piece = data[start:end].decode()
        if not trim(piece): whitespace += piece
        else: pieces.append(whitespace + piece); whitespace = ""
        start = end; start_index = bisect.bisect_left(ends, end) + 1
    if whitespace and pieces: pieces[-1] += whitespace
    parts, offset = [], 0
    for identity, body in enumerate(pieces):
        end = offset + len(body.encode())
        parts.append(dict(id=identity, utf8Start=offset, utf8End=end, source=body, state="pending"))
        offset = end
    return parts
