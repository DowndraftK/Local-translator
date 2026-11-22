"""One isolated, versioned UI draft. Synchronous atomic writes cannot arrive late."""
import json
import uuid
from pathlib import Path

from PySide6.QtCore import QLockFile
from translator_core.contracts import MAX_INPUT_BYTES, sha, task_id
from translator_core.store import atomic_write, safe_file


class TextDraftStore:
    def __init__(self, task_root):
        root = Path(task_root).resolve()
        self.owner = sha(str(root).encode())
        self.path = root.parent / (root.name + "-text-draft.json")
        self.lock = QLockFile(str(self.path)+".lock")
        self.lock.setStaleLockTime(0)
        self.locked = False
        self.revision = 0
        self.warning = ""
        self.corrupt = None
        self.conflicted = False

    def load(self):
        try:
            if not self.path.exists(): return None
            raw = safe_file(self.path, MAX_INPUT_BYTES*8)
            data = json.loads(raw)
            if (data['schema'] != 1 or data['owner'] != self.owner or
                    type(data['revision']) is not int or data['revision'] < 1):
                raise ValueError('草稿版本或归属无效')
            d = data['draft']
            if (not isinstance(d['text'], str) or len(d['text'].encode()) > MAX_INPUT_BYTES or
                    d['direction'] not in ('en-zh', 'zh-en') or d['mode'] not in (0, 1) or
                    any(type(d[k]) is not int or d[k] < 0 for k in ('position', 'anchor'))):
                raise ValueError('草稿内容无效')
            context = data.get('context')
            if context is not None:
                task_id(context['task_id'])
                if any(type(context[k]) is not int or context[k] < 0 for k in ('position', 'anchor', 'scroll')):
                    raise ValueError('阅读位置无效')
                if context.get('mode',0) not in (0,1) or type(context.get('suppressed',False)) is not bool:
                    raise ValueError('阅读模式无效')
            self.revision = data['revision']
            return data
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.warning = '草稿未能恢复，原文件保留：'+str(exc)
            # Preserve the exact unreadable bytes before a subsequent explicit edit.
            self.corrupt = self.path
            return None

    def save(self, draft, context):
        if self.conflicted: raise ValueError('草稿已由另一窗口更新，未覆盖磁盘，请先保留当前输入并重新打开')
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError('草稿路径包含符号链接，未写入')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.locked:
            if not self.lock.tryLock(0): raise ValueError('另一窗口正在保存这份草稿，请先关闭该窗口')
            self.locked = True
            # Acquire before checking disk revision; a formerly idle UI cannot overwrite a newer draft.
            if self.path.exists() and self.corrupt is None:
                current = json.loads(safe_file(self.path, MAX_INPUT_BYTES*8))
                if current.get('owner') != self.owner or current.get('revision') != self.revision:
                    self.conflicted = True
                    self.lock.unlock(); self.locked = False
                    raise ValueError('草稿已被另一窗口更新，保留当前输入且未覆盖磁盘')
        if len(draft['text'].encode()) > MAX_INPUT_BYTES:
            raise ValueError('未提交原文超过1MiB，草稿尚未保存，请减少内容或手动复制备份')
        if self.corrupt:
            raw = safe_file(self.corrupt, MAX_INPUT_BYTES*8)
            backup = self.path.with_name(self.path.name+'.unreadable-'+uuid.uuid4().hex)
            atomic_write(raw, backup)
            self.warning += '；原文件副本：'+str(backup)
            self.corrupt = None
        data = dict(schema=1, owner=self.owner, revision=self.revision+1, draft=draft, context=context)
        atomic_write(json.dumps(data, ensure_ascii=False, allow_nan=False).encode(), self.path)
        self.revision += 1

    def close(self):
        if self.locked: self.lock.unlock(); self.locked = False
