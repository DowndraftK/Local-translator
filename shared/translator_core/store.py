"""Schema1 durable snapshots in an explicitly separate, single-writer preview root."""
import os
import hashlib
import shutil
import sys
import threading
import uuid
from copy import deepcopy
from pathlib import Path, PureWindowsPath

from . import CoreError
from .contracts import (MAX_INPUT_BYTES, MAX_SNAPSHOT_BYTES, decode_snapshot,
                        encode_snapshot, sha, task_id)

MARKER = b"LocalTranslator B02 isolated text store v1\n"
MAX_DOCUMENT_BYTES = 256_000_000


def file_identity(path, limit=MAX_DOCUMENT_BYTES, cancel=None):
    """Bounded streaming hash; a changed/opened source is never accepted silently."""
    path = Path(path)
    if is_link(path) or not path.is_file():
        raise CoreError("来源文件缺失或路径不安全。", "unsafe_path")
    digest = hashlib.sha256(); count = 0
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if before.st_size > limit: raise CoreError("文件超出256,000,000字节或本任务容量上限。", "resource_limit")
        while block := handle.read(65536):
            if cancel and cancel.is_set():
                from . import Cancelled
                raise Cancelled()
            count += len(block)
            if count > limit: raise CoreError("文件超过容量上限。", "resource_limit")
            digest.update(block)
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or count != before.st_size:
        raise CoreError("读取时来源发生变化，未封存。", "source_changed")
    return count, digest.hexdigest()


def atomic_copy(source, target, count, fingerprint, cancel=None):
    """Copy immutable assets with constant memory and verify before publication."""
    source, target = Path(source), Path(target)
    if is_link(source) or not source.is_file() or is_link(target) or is_link(target.parent):
        raise CoreError("封存路径不安全。", "unsafe_path")
    temp = target.with_name(".copy-" + uuid.uuid4().hex)
    digest = hashlib.sha256(); obtained = 0
    try:
        with source.open("rb") as reader, temp.open("xb") as writer:
            while block := reader.read(65536):
                if cancel and cancel.is_set():
                    from . import Cancelled
                    raise Cancelled()
                obtained += len(block)
                if obtained > count or obtained > MAX_DOCUMENT_BYTES:
                    raise CoreError("来源大小变化或超限。", "source_mismatch")
                digest.update(block); writer.write(block)
            writer.flush(); os.fsync(writer.fileno())
        if obtained != count or digest.hexdigest() != fingerprint:
            raise CoreError("来源字节/hash不匹配，未封存。", "source_mismatch")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def is_link(path):
    path = Path(path)
    if path.is_symlink(): return True
    # NTFS junctions are directory reparse points, not Python symbolic links.
    if sys.platform == "win32":
        return any(p.is_junction() or p.is_symlink() for p in (path, *path.parents))
    return False


def safe_file(path, limit=MAX_SNAPSHOT_BYTES * 2):
    path = Path(path)
    if is_link(path) or not path.is_file():
        raise CoreError("文件缺失或为符号链接，原数据保留。", "unsafe_path")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise CoreError("文件超过此预览的容量上限。")
    return data


def atomic_write(data, path):
    path = Path(path)
    if is_link(path) or is_link(path.parent):
        raise CoreError("拒绝替换符号链接。", "unsafe_path")
    temp = path.with_name(".write-" + uuid.uuid4().hex)
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
        if sys.platform != "win32":
            directory = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


class TaskStore:
    def __init__(self, root, write=atomic_write):
        self.root = Path(root).absolute()
        self.write = write
        self.high_water = {}
        self.expected_snapshots = {}
        self.history_epoch = 0
        self.mutex = threading.RLock()
        if is_link(self.root):
            raise CoreError("预览任务根不能是符号链接。", "unsafe_path")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = self.root / ".b02-preview-store"
        if not marker.exists():
            if any(self.root.iterdir()):
                raise CoreError("请选择新的空预览目录，不能直接接管旧任务目录。", "root_not_isolated")
            atomic_write(MARKER, marker)
        if safe_file(marker, 200) != MARKER:
            raise CoreError("预览目录身份不匹配。", "root_not_isolated")
        lock_path = self.root / ".writer.lock"
        if is_link(lock_path):
            raise CoreError("任务锁路径无效。", "unsafe_path")
        self.lock = lock_path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                # Do not append a byte on every reopen or touch a byte another
                # writer has locked. The first writer initializes only an empty file.
                self.lock.seek(0, os.SEEK_END)
                if self.lock.tell() == 0:
                    self.lock.write(b"0"); self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            raise CoreError("另一预览正在写此目录，请先安全关闭。", "writer_busy") from exc
        for name in ("incoming", "exports"):
            folder = self.root / name
            if is_link(folder):
                self.close(); raise CoreError("预览目录包含不安全路径。", "unsafe_path")
            folder.mkdir(exist_ok=True, mode=0o700)

        from .history import HistoryRepository
        self.history = HistoryRepository(self)

    def close(self):
        if getattr(self, "lock", None) and not self.lock.closed:
            self.lock.close()

    def directory(self, identity):
        result = self.root / task_id(identity)
        if is_link(result):
            raise CoreError("任务目录不能是符号链接。", "unsafe_path")
        return result

    @staticmethod
    def asset_name(task):
        asset = task.get("asset")
        if asset is None:
            return None
        if not isinstance(asset, dict):
            raise CoreError("源副本字段无效。")
        name = asset.get("filename")
        if (not isinstance(name, str) or not name or name in (".", "..") or
                any(c in name for c in ("/", "\\", ":", "\0")) or
                name.casefold() in ("current.json", "previous.json") or name.startswith(".") or
                name.endswith((".", " ")) or PureWindowsPath(name).is_reserved()):
            raise CoreError("源副本路径无效。", "unsafe_path")
        return name

    @classmethod
    def verify_asset(cls, task, directory):
        name = cls.asset_name(task)
        if name is None:
            return None
        asset = task["asset"]
        if task.get("kind") in ("文档", '图片', '音频'):
            path = Path(directory) / name
            from .images import MAX_SOURCE_BYTES
            if task['kind'] in ('图片', '音频') and any(is_link(p) for p in (path,*path.parents)):
                raise CoreError('图片封存路径包含链接。','unsafe_path')
            count, fingerprint = file_identity(path, MAX_SOURCE_BYTES if task['kind']=='图片' else MAX_DOCUMENT_BYTES)
            if type(asset.get("byteCount")) is not int or count != asset["byteCount"] or fingerprint != asset.get("fingerprint"):
                raise CoreError("封存源副本不匹配；仍可阅读/导出，禁止提取、识别或继续。", "source_mismatch")
            return path
        data = safe_file(Path(directory) / name, MAX_INPUT_BYTES)
        if type(asset.get("byteCount")) is not int or len(data) != asset["byteCount"] or sha(data) != asset.get("fingerprint"):
            raise CoreError("封存源副本不匹配；仍可阅读/导出，禁止继续。", "source_mismatch")
        return data

    @staticmethod
    def read_directory(directory, identity):
        directory = Path(directory)
        try:
            task = decode_snapshot(safe_file(directory / "current.json"))
        except CoreError as exc:
            if exc.code in ("future_schema", "unsafe_path") and (directory / "current.json").exists():
                raise
            try:
                task = decode_snapshot(safe_file(directory / "previous.json"))
            except CoreError:
                raise exc
            if task_id(task["id"]) != task_id(identity):
                raise CoreError("保存目录与任务身份不匹配。")
            return task, "最新快照损坏；已只读打开上一份有效快照。原文件保留，请导出后另建任务。"
        if task_id(task["id"]) != task_id(identity):
            raise CoreError("保存目录与任务身份不匹配。")
        return task, None

    def read(self, identity):
        with self.mutex:
            task, warning = self.read_directory(self.directory(identity), identity)
            self.high_water[task_id(identity)] = max(task["revision"], self.high_water.get(task_id(identity), 0))
            try: self.verify_asset(task, self.directory(identity))
            except (CoreError,OSError) as exc: warning = (warning + "\n" if warning else "") + str(exc)
            current = self.directory(identity) / "current.json"
            if current.is_file(): self.expected_snapshots[task_id(identity)] = sha(safe_file(current))
            return deepcopy(task), warning

    def list(self):
        # Legacy callers use the same authority/summary mapping as the two UI views.
        query = dict(version=self.history.description()['contract'],
            owner=dict(root_id=self.history.root_id,session_id=self.history.session_id,
                scope='history',view_revision=0,query_id=str(uuid.uuid4())),limit=20)
        result=self.history.query(query)
        return result['items'], [e['task_id']+'：'+e['message'] for e in result['errors']]

    def save(self, task, asset_data=None):
        encoded = encode_snapshot(task)
        identity = task_id(task["id"])
        with self.mutex:
            directory = self.directory(identity); current = directory / "current.json"
            existing = None
            if current.exists() or is_link(current):
                existing = safe_file(current)
                expected = self.expected_snapshots.get(identity)
                if expected is not None and sha(existing) != expected:
                    raise CoreError("磁盘任务已由外部替换，保留内存结果且拒绝覆盖。", "source_changed")
                previous = decode_snapshot(existing)
                if task_id(previous["id"]) != identity:
                    raise CoreError("任务目录身份不匹配。")
                self.high_water[identity] = max(previous["revision"], self.high_water.get(identity, 0))
            if task["revision"] <= self.high_water.get(identity, 0):
                return False
            directory.mkdir(exist_ok=True, mode=0o700)
            name = self.asset_name(task)
            if name:
                asset = directory / name
                if not asset.exists() and not is_link(asset):
                    metadata = task["asset"]
                    if isinstance(asset_data, Path) and task["kind"] in ("文档", '图片', '音频'):
                        atomic_copy(asset_data, asset, metadata["byteCount"], metadata["fingerprint"])
                    elif asset_data is None or len(asset_data) != metadata["byteCount"] or sha(asset_data) != metadata["fingerprint"]:
                        raise CoreError("缺少匹配的封存源副本。", "source_mismatch")
                    else: self.write(asset_data, asset)
                self.verify_asset(task, directory)
            if existing is not None:
                self.write(existing, directory / "previous.json")
            self.write(encoded, current)
            self.high_water[identity] = task["revision"]
            self.expected_snapshots[identity] = sha(encoded)
            self.history_epoch += 1
            return True

    def import_copy(self, source):
        """Explicit read-only source selection, preserving exact original snapshot bytes."""
        source = Path(source).absolute()
        if is_link(source) or not source.is_dir():
            raise CoreError("请选择实际任务目录，不能选择符号链接。", "unsafe_path")
        identity = task_id(source.name)
        task, warning = self.read_directory(source, identity)
        # Do not quietly repair or replace a corrupt source. A read-only import can
        # retain it alongside previous; it remains blocked for continuation.
        asset_data = self.verify_asset(task, source)
        with self.mutex:
            target = self.directory(identity)
            if target.exists():
                raise CoreError("此UUID已在预览目录中，未覆盖既有任务。", "task_exists")
            temp = self.root / (".import-" + uuid.uuid4().hex)
            temp.mkdir(mode=0o700)
            try:
                for name in ("current.json", "previous.json"):
                    if (source / name).exists():
                        atomic_write(safe_file(source / name), temp / name)
                if asset_data is not None:
                    if isinstance(asset_data, Path):
                        atomic_copy(asset_data, temp / self.asset_name(task), task["asset"]["byteCount"], task["asset"]["fingerprint"])
                    else: atomic_write(asset_data, temp / self.asset_name(task))
                if task['kind'] == '音频' and task['audio']['pcm']:
                    pcm = task['audio']['pcm']; media = source / pcm['filename']
                    from .resources import no_links
                    no_links(media)
                    if file_identity(media, 57_600_044) != (pcm['byteCount'], pcm['fingerprint']):
                        raise CoreError('导入任务的规范音频不匹配。', 'source_mismatch')
                    atomic_copy(media, temp / pcm['filename'], pcm['byteCount'], pcm['fingerprint'])
                capture = (task.get('audio') or {}).get('recording')
                if capture:
                    (temp / 'capture').mkdir()
                    from .resources import no_links
                    for chunk in capture['chunks']:
                        original = source / chunk['file']
                        no_links(original)
                        atomic_copy(original, temp / chunk['file'], chunk['bytes'], chunk['sha256'])
                os.rename(temp, target)
                self.history_epoch += 1
            finally:
                if temp.exists(): shutil.rmtree(temp)
        return task, warning

    def incoming(self, name, fingerprint, count, allow_empty=False):
        if (not isinstance(name, str) or len(name) != 36 or not name.endswith(".txt") or
                any(c not in "0123456789abcdef" for c in name[:-4]) or
                type(count) is not int or not (0 if allow_empty else 1) <= count <= MAX_INPUT_BYTES):
            raise CoreError("输入暂存身份无效。", "unsafe_path")
        path = self.root / "incoming" / name
        data = safe_file(path, MAX_INPUT_BYTES)
        if len(data) != count or sha(data) != fingerprint:
            raise CoreError("输入暂存内容不匹配。", "source_mismatch")
        try: text = data.decode("utf-8")
        except UnicodeError as exc: raise CoreError("输入不是UTF-8文字。") from exc
        path.unlink()
        return text
