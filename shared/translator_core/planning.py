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
    splitter_version = "natural-language-utf8-v1"

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


class QtBoundaries:
    """Fixed Qt Unicode facts for Windows; shared split() still owns packing.

    This is a distinct algorithm identity, never an imitation of NLTokenizer.
    Offsets from Qt are UTF-16 units and must be mapped to UTF-8 explicitly.
    """
    splitter_version = "qt6102-unicode-utf8-v1"

    def __init__(self):
        from PySide6.QtCore import QTextBoundaryFinder, qVersion
        if qVersion() != "6.10.2":
            raise CoreError("Windows分句需要固定Qt6.10.2。", "boundary_adapter_unavailable")
        self.finder = QTextBoundaryFinder

    def __call__(self, source, cancel):
        if cancel.is_set(): raise Cancelled()
        offsets = {0: (0, 0)}
        utf16 = utf8 = 0
        for index, char in enumerate(source):
            utf16 += 2 if ord(char) > 0xffff else 1
            utf8 += len(char.encode())
            offsets[utf16] = (utf8, index + 1)

        def positions(kind):
            finder = self.finder(kind, source)
            result = []
            while True:
                if cancel.is_set(): raise Cancelled()
                end = finder.toNextBoundary()
                if end < 0: return result
                if end not in offsets:
                    raise CoreError("Qt边界落在UTF-16代理对中。")
                result.append(end)

        sentence_ends = set(positions(self.finder.Sentence))
        # Unicode White_Space, pinned here rather than Python's broader isspace.
        whitespace = "\t\n\v\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
        newlines = "\n\r\v\f\u0085\u2028\u2029"
        rows, start = [], 0
        for end in positions(self.finder.Grapheme):
            byte_end, index = offsets[end]
            cluster = source[start:index]
            rows.append([byte_end, int(all(c in newlines for c in cluster)),
                         int(all(c in whitespace for c in cluster)), int(end in sentence_ends)])
            start = index
        return rows


def platform_boundaries(executable=None):
    if sys.platform == "darwin": return MacBoundaries(executable or "")
    if sys.platform == "win32": return QtBoundaries()
    raise CoreError("此预览仅接原生Mac/Windows。", "boundary_adapter_unavailable")


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
