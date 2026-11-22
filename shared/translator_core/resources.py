"""One bounded, read-only resource contract. No speech/Qt/GPU imports here."""
import hashlib
import json
import math
import os
import platform
import stat
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path, PurePosixPath

from . import CoreError
from .contracts import default_recipe, strict_json

CONTRACT = "local-resources-v1"
REFERENCE = "resource-reference-set-v1"
SELECTION = "resource-selection-v1"
CHECK = "resource-check-v1"
BINDING = "resource-binding-v1"
SPEECH_BINDING = "speech-preset-binding-v1"
MANIFEST_LIMIT = 8 * 1024 * 1024
SUMMARY_LIMIT = 512 * 1024
FILE_LIMIT = 8 * 1024**3
SET_LIMIT = 16 * 1024**3
FILE_COUNT = 20000
CHUNK = 1024 * 1024
CHECK_SECONDS = 600
FILE_SECONDS = 120
EVIDENCE_SECONDS = 86400
REQUIRED = {
    "speech.file_asr": ("speech.runtime", "speech.backend", "speech.model", "speech.assets", "speech.bridge"),
    "speech.continuous_asr": ("speech.runtime", "speech.backend", "speech.model", "speech.assets", "speech.bridge"),
    "speech.refine": ("speech.runtime", "speech.backend", "speech.model", "speech.assets", "speech.bridge"),
    "audio.decode": ("speech.runtime", "speech.bridge"),
    "audio.playback": ("mac.frameworks",),
}
EXECUTION = dict(language="en", encoder="mlx", decoder="mps", decoder_dtype="float32",
                 context_tokens=128, cpu_fallback=False, refinement="mlx-whisper")


def valid_speech_execution(capability, value, resources=None):
    if value == EXECUTION:
        return True
    if not isinstance(value, dict) or not capability.startswith('speech.'):
        return False
    language = value.get('language')
    from .languages import LANGUAGES
    from .resource_macos import LEGACY_WHISPER, whisper_variant
    model = (resources or {}).get('speech.model', {})
    return (isinstance(language,str) and language in LANGUAGES and value==dict(EXECUTION,language=language)
            and whisper_variant(model.get('manifest',{}).get('sha256'))==LEGACY_WHISPER)
SPEC_KEYS = {"resource_id", "role", "version", "platform", "architecture", "root", "manifest",
             "origin", "license", "distribution_status", "ownership", "link_policy"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def manifest_hasher(expected):
    if "git_blob" in expected:
        # Small files in pinned Hub revisions use Git's blob identity.
        return hashlib.sha1(f"blob {expected['size']}\0".encode("ascii"), usedforsecurity=False)
    return hashlib.sha256()


def manifest_matches(expected, hashed):
    return hashed.hexdigest() == expected.get("sha256", expected.get("git_blob"))


class ResourceError(CoreError):
    def __init__(self, code, message, *, resource_id=None, capability=None, retryable=False,
                 next_action="请重新选择合法的现有资源引用，再主动检查。"):
        super().__init__(message, code, False)
        self.retryable = retryable
        self.resource_id, self.capability, self.next_action = resource_id, capability, next_action

    def wire(self):
        return dict(code=self.code, message=str(self), retryable=self.retryable,
                    resource_id=self.resource_id, capability=self.capability, next_action=self.next_action)


def token(path, *, follow=False):
    st = os.stat(path, follow_symlinks=follow)
    return [st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def no_links(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or len(str(path).encode()) > 4096:
        raise ResourceError("unsafe_path", "资源路径必须是明确、安全的绝对路径。")
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise ResourceError("unsafe_path", "资源根或清单的祖先含未授权链接。")
    return path


def safe_read(path, limit=MANIFEST_LIMIT):
    path = no_links(path)
    fd = open_readonly(path)
    with os.fdopen(fd, "rb") as stream:
        before = token(path)
        if not stat.S_ISREG(before[2]) or before[3] > limit:
            raise ResourceError("resource_limit", "资源清单不是普通文件或超过大小上限。")
        raw = stream.read(limit + 1)
        if len(raw) > limit or before != token(path) or before != stat_token(os.fstat(stream.fileno())):
            raise ResourceError("resource_changed", "读取期间资源清单发生变化；请重新检查。")
    return raw


def open_readonly(path):
    """Anchor every ancestor with openat/NOFOLLOW, closing a rename race."""
    path=Path(path)
    parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent);parent=child
        return os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally:os.close(parent)


def verifier_identity():
    # Small reviewed code identity only; never a resource/model scan.
    return digest({name:hashlib.sha256(safe_read(Path(__file__).parent/name,SUMMARY_LIMIT)).hexdigest()
                   for name in ('resources.py','resource_macos.py','resource_macos_probe.py','resource_gui_probe.py','mac_sandbox.py','dictionary/resources.py')})


def stat_token(st):
    return [st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def json_value(raw):
    try:
        return strict_json(raw)
    except (CoreError, ValueError, TypeError, UnicodeError) as exc:
        raise ResourceError("resource_incompatible", "资源清单格式损坏或包含重复字段。") from exc


def hash_value(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ResourceError("resource_incompatible", "资源校验身份无效。")
    return value


def bounded_string(value, limit=1024):
    if not isinstance(value, str) or not value or len(value.encode()) > limit or any(ord(c) < 32 for c in value):
        raise ResourceError("resource_incompatible", "资源文字字段为空、过长或含控制字符。")
    return value


def relative_name(name):
    bounded_string(name, 4096)
    if (name.startswith("/") or "\\" in name or ":" in name or
            any(p in ("", ".", "..") for p in name.split("/")) or
            any(p.endswith(".incomplete") for p in PurePosixPath(name).parts)):
        raise ResourceError("unsafe_path", "资源清单含越界、未完成或不安全路径。")
    return name


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS:
        raise ResourceError("resource_incompatible", "资源描述字段不完整或版本不受支持。")
    for key in SPEC_KEYS - {"manifest"}:
        bounded_string(spec[key], 4096 if key == "root" else 1024)
    no_links(spec["root"])
    manifest = spec["manifest"]
    if not isinstance(manifest, dict) or set(manifest) != {"path", "bytes", "sha256", "format"}:
        raise ResourceError("resource_incompatible", "资源缺少完整清单身份。")
    no_links(manifest["path"]); hash_value(manifest["sha256"])
    if type(manifest["bytes"]) is not int or not 0 < manifest["bytes"] <= MANIFEST_LIMIT:
        raise ResourceError("resource_limit", "资源清单大小超限。")
    if manifest["format"] not in ("component-v1", "model-v0", "assets-v1", "resource-files-v1"):
        raise ResourceError("resource_incompatible", "资源清单版本尚不支持。")
    if spec["ownership"] != "external_readonly" or spec["link_policy"] not in ("deny", "manifest_internal"):
        raise ResourceError("resource_incompatible", "本包只接受现有资源的只读引用。")
    if spec["link_policy"] == "manifest_internal" and manifest["format"] != "component-v1":
        raise ResourceError("unsafe_path", "只有封存组件清单可显式声明内部链接。")
    return deepcopy(spec)


def content_identity(spec):
    value = {k: v for k, v in spec.items() if k != "root"}
    value["manifest"] = {k: v for k, v in spec["manifest"].items() if k != "path"}
    return digest(value)


def read_reference(path):
    raw = safe_read(path, SUMMARY_LIMIT)
    value = json_value(raw)
    if (not isinstance(value, dict) or set(value) != {"version", "title", "resources"} or
            value["version"] != REFERENCE or not isinstance(value["resources"], list) or
            not 1 <= len(value["resources"]) <= 16):
        raise ResourceError("resource_incompatible", "资源引用文件损坏、版本过新或项目数超限。")
    bounded_string(value["title"], 256)
    specs = [validate_spec(s) for s in value["resources"]]
    identities = {s["resource_id"]: content_identity(s) for s in specs}
    if len(identities) != len(specs):
        raise ResourceError("resource_incompatible", "资源逻辑身份重复。")
    return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                token=token(path), title=value["title"], resources=specs,
                resource_set_id=digest(identities))


def manifest_entries(spec):
    manifest = spec["manifest"]
    raw = safe_read(manifest["path"])
    if len(raw) != manifest["bytes"] or hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise ResourceError("resource_corrupt", "资源清单的大小或校验值不匹配。")
    value = json_value(raw); fmt = manifest["format"]
    if not isinstance(value, dict):
        raise ResourceError("resource_incompatible", "资源清单必须是对象。")
    if fmt == "component-v1":
        if (type(value.get("schema")) is not int or value["schema"] != 1 or
                value.get("version") != spec["version"] or value.get("platform") != spec["platform"]):
            raise ResourceError("resource_incompatible", "封存组件版本或平台不匹配。")
    elif fmt == "model-v0":
        if set(value) != {"repo", "revision", "files"} or value.get("revision") != spec["version"]:
            raise ResourceError("resource_incompatible", "模型版本不匹配或格式过新。")
    elif fmt == "assets-v1":
        if set(value) != {"schema", "id", "files"} or type(value["schema"]) is not int or value["schema"] != 1:
            raise ResourceError("resource_incompatible", "资产清单版本不受支持。")
    elif set(value) != {"version", "files"} or value.get("version") != "resource-files-v1":
        raise ResourceError("resource_incompatible", "通用资源清单版本不受支持。")
    files = value.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= FILE_COUNT:
        raise ResourceError("resource_limit", "资源清单为空或文件数超限。")
    total = 0
    for name, item in files.items():
        relative_name(name)
        if not isinstance(item, dict):
            raise ResourceError("resource_incompatible", "文件清单项无效。")
        if set(item) == {"link"}:
            if spec["link_policy"] != "manifest_internal":
                raise ResourceError("unsafe_path", "资源含未授权链接。")
            bounded_string(item["link"], 4096)
            if item["link"].startswith("/") or "\\" in item["link"]:
                raise ResourceError("unsafe_path", "组件链接不可指向绝对或外部路径。")
        elif set(item) == {"size", "sha256"} or fmt == "model-v0" and set(item) == {"size", "git_blob"}:
            if "sha256" in item:
                hash_value(item["sha256"])
            else:
                blob = item["git_blob"]
                if not isinstance(blob, str) or len(blob) != 40 or any(c not in "0123456789abcdef" for c in blob):
                    raise ResourceError("resource_incompatible", "模型 Git 文件校验值不合法。")
            if type(item["size"]) is not int or not 0 <= item["size"] <= FILE_LIMIT:
                raise ResourceError("resource_limit", "资源文件大小超限。")
            total += item["size"]
        else:
            raise ResourceError("resource_incompatible", "文件清单项缺少大小或完整校验值。")
        if total > SET_LIMIT:
            raise ResourceError("resource_limit", "资源累计大小超限。")
    return files, token(manifest["path"]), total


def stop_check(cancel, deadline):
    if cancel.is_set():
        raise ResourceError("check_cancelled", "检查已取消，先前核验记录仍保留。", retryable=True)
    if time.monotonic() >= deadline:
        raise ResourceError("check_timeout", "检查超过时间上限，先前核验记录仍保留。", retryable=True)


def directory_inventory(spec, files, cancel, deadline):
    """Inspect only an explicitly selected manifest's own resource root."""
    fmt=spec['manifest']['format']
    if fmt not in ('component-v1','model-v0','assets-v1'): return {}
    root=Path(spec['root']);observed=set();directories={}
    for base,dirs,names in os.walk(root,followlinks=False):
        stop_check(cancel,deadline)
        base=Path(base);directories[str(base.relative_to(root))]=token(base)
        if len(directories)>FILE_COUNT:
            raise ResourceError('resource_limit','资源目录数超过检查上限。')
        for name in tuple(dirs):
            path=base/name
            if path.is_symlink():dirs.remove(name);names.append(name)
        for name in names:
            path=base/name;relative=str(path.relative_to(root));relative_name(relative)
            if path.is_symlink() and relative not in files:
                raise ResourceError('unsafe_path','资源中发现未登记链接：'+relative)
            observed.add(relative)
            if len(observed)>FILE_COUNT+1:
                raise ResourceError('resource_limit','资源目录实际文件数超限。')
    if fmt in ('component-v1','model-v0'):
        allowed=set(files)
        manifest=Path(spec['manifest']['path'])
        if manifest.is_relative_to(root):allowed.add(str(manifest.relative_to(root)))
        if observed!=allowed:
            raise ResourceError('resource_corrupt','资源目录含清单外文件或缺少文件；请恢复匹配的原资源。')
    return directories


def verify_resource(spec, cancel, deadline, progress):
    root = no_links(spec["root"])
    if not root.is_dir():
        raise ResourceError("resource_missing", "找不到已引用的资源目录。")
    if spec["platform"] not in ("any", "macos-arm64") or spec["architecture"] not in ("any", platform.machine()):
        raise ResourceError("resource_incompatible", "所选资源不适用于当前平台或架构。")
    files, manifest_token, size = manifest_entries(spec)
    directories=directory_inventory(spec,files,cancel,deadline)
    records, actual_bytes = {}, 0
    for index, (name, expected) in enumerate(files.items()):
        stop_check(cancel, deadline)
        path = root / name
        no_links(path.parent)
        if "link" in expected:
            if not path.is_symlink() or os.readlink(path) != expected["link"]:
                raise ResourceError("unsafe_path", "封存组件链接与清单不匹配。")
            try:
                final = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ResourceError('unsafe_path', '组件链接无法安全解析。') from exc
            if not final.is_relative_to(root) or final == root:
                raise ResourceError("unsafe_path", "组件链接越出了已引用的只读范围。")
            target = str(final.relative_to(root))
            protected_directory = (final.is_dir() and any(
                key.startswith(target+'/') and 'sha256' in value for key, value in files.items()))
            if not protected_directory and (target not in files or "sha256" not in files[target]):
                raise ResourceError("unsafe_path", "组件链接没有受清单保护的最终文件。")
            records[name] = dict(token=token(path), target=target, target_token=token(final))
            continue
        no_links(path)
        fd = open_readonly(path)
        with os.fdopen(fd, "rb") as stream:
            before = stat_token(os.fstat(stream.fileno()))
            if not stat.S_ISREG(before[2]) or before[3] != expected["size"]:
                raise ResourceError("resource_corrupt", "资源文件类型或大小不匹配：" + name)
            file_deadline = min(deadline, time.monotonic() + FILE_SECONDS)
            hashed = manifest_hasher(expected); count = 0
            while True:
                stop_check(cancel, file_deadline)
                block = stream.read(CHUNK)
                if not block:
                    break
                hashed.update(block); count += len(block); actual_bytes += len(block)
                if actual_bytes > SET_LIMIT or count > expected["size"]:
                    raise ResourceError("resource_limit", "资源读取超过已声明边界。")
                progress(spec["resource_id"], index, len(files), actual_bytes)
            if count != expected["size"] or not manifest_matches(expected, hashed):
                raise ResourceError("resource_corrupt", "资源文件校验不匹配：" + name)
            if before != stat_token(os.fstat(stream.fileno())) or before != token(path):
                raise ResourceError("resource_changed", "核验期间资源文件发生变化：" + name)
            records[name] = dict(token=before, **{k: v for k, v in expected.items() if k != "size"})
    result = dict(identity=content_identity(spec), state="files_verified", files=len(files),
                  bytes=actual_bytes, manifest_token=manifest_token, records=records,
                  root_token=token(root), directory_tokens=directories, model_load="not_tested", inference="not_tested")
    if not tokens_match(spec, result):
        raise ResourceError("resource_changed", "核验结束前资源被替换，请重新检查。")
    return result


def tokens_match(spec, evidence):
    try:
        no_links(spec["root"]); no_links(spec["manifest"]["path"])
        if (evidence["identity"] != content_identity(spec) or
                token(spec["manifest"]["path"]) != evidence["manifest_token"] or
                token(spec["root"]) != evidence["root_token"]):
            return False
        root = Path(spec["root"])
        for name,expected in evidence.get('directory_tokens',{}).items():
            if name!='.':relative_name(name)
            if token(root/name)!=expected:return False
        for name, item in evidence["records"].items():
            relative_name(name); path = root / name; no_links(path.parent)
            if token(path) != item["token"]:
                return False
            if "target" in item:
                relative_name(item["target"])
                if (not path.is_symlink() or path.resolve(strict=True) != root / item["target"] or
                        token(root / item["target"]) != item["target_token"]):
                    return False
        return True
    except (OSError, CoreError, KeyError, TypeError, RuntimeError):
        return False


def translate_error(exc, resource_id=None):
    if isinstance(exc, ResourceError):
        error = exc.wire(); error["resource_id"] = resource_id or error["resource_id"]
        return error
    code, message = ("permission_denied", "没有读取资源的权限。") if isinstance(exc, PermissionError) else (
        ("resource_missing", "引用的资源文件不存在。") if isinstance(exc, FileNotFoundError) else
        ("check_failed", "资源检查失败：" + str(exc)[:512]))
    return ResourceError(code, message, resource_id=resource_id, retryable=True).wire()


def atomic_json(path, value):
    """Own-root writes only; caller checks the expected disk baseline first."""
    path = no_links(path); no_links(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = encoded(value)
    if len(raw) > 32 * 1024 * 1024:
        raise ResourceError("resource_limit", "资源证据超过保存上限。")
    temporary = path.with_name(".resource-" + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        if safe_read(path, 32 * 1024 * 1024) != raw:
            raise ResourceError("selection_save_failed", "资源配置写入后确认失败，请刷新核对。")
    finally:
        temporary.unlink(missing_ok=True)


class ResourceManager:
    """Single worker-owned selection; full checks run on its existing operation."""
    def __init__(self, data_root, dictionary=None, adapters=None, probe=None):
        self.root = no_links(Path(data_root).absolute()) / "resources"
        no_links(self.root); self.root.mkdir(parents=True, exist_ok=True)
        self.config = self.root / "selection.json"
        self.dictionary, self.adapters, self.probe = dictionary, adapters or {}, probe
        self.service_evidence = None
        self.selection = self.saved_selection = None
        self.revision = self.generation = 0
        self.dirty = False; self.warning = None; self.last = None; self.attempt = None
        self.config_blocked = False
        self.config_baseline = None
        try:
            self.config_baseline = safe_read(self.config, SUMMARY_LIMIT) if self.config.exists() or self.config.is_symlink() else None
            if self.config_baseline is not None:
                value = json_value(self.config_baseline)
                if (not isinstance(value, dict) or set(value) != {"version", "revision", "reference"} or
                        value["version"] != SELECTION or type(value["revision"]) is not int or value["revision"] < 1):
                    raise ResourceError("resource_incompatible", "保存的资源配置损坏或版本过新；原文件保留。")
                selected = read_reference(value["reference"]["path"])
                if selected["sha256"] != value["reference"]["sha256"]:
                    raise ResourceError("resource_changed", "保存的资源引用已改变，请明确重新选择。")
                self.selection = self.saved_selection = selected; self.revision = value["revision"]
        except (CoreError, OSError, KeyError, TypeError) as exc:
            self.warning = translate_error(exc); self.config_blocked = True
        try:
            cache = self.root / "last-check.json"
            if cache.exists() or cache.is_symlink():
                value = json_value(safe_read(cache, 32 * 1024 * 1024))
                if not valid_receipt(value, self.root):
                    raise ResourceError("resource_incompatible", "资源检查收据损坏或版本不受支持。")
                self.last = value
        except (CoreError, OSError, KeyError, TypeError) as exc:
            self.warning = translate_error(exc)

    def select(self, path):
        selected = read_reference(no_links(Path(path)))
        self.revision += 1; self.generation += 1
        self.selection = selected; self.dirty = True; self.last = None; self.attempt = None
        self.warning = None
        return self.describe()

    def refresh_reference_token(self):
        """Native file panels may add access metadata after returning a path.

        A bounded reference is content, unlike the large resources it names.
        Revalidate its exact bytes before accepting a metadata-only token change;
        ordinary resource/model tokens never receive this exception.
        """
        if not self.selection:return
        if token(self.selection['path'])==self.selection['token']:return
        current=read_reference(self.selection['path'])
        if current['sha256']!=self.selection['sha256']:
            raise ResourceError('resource_changed','所选引用文件内容发生变化，请重新选择。')
        self.selection['token']=current['token']
        if self.saved_selection and (self.saved_selection['path'],self.saved_selection['sha256'])==(current['path'],current['sha256']):
            self.saved_selection['token']=current['token']

    def save(self):
        if self.config_blocked:
            raise ResourceError('selection_save_failed','现有配置损坏、版本过新或无法读取，原文件保留。请先在外部保留该文件并选择新的本包数据目录。')
        if not self.selection:
            raise ResourceError("resource_missing", "请先选择现有资源引用文件。")
        if self.warning and self.config_baseline is None and self.config.exists():
            raise ResourceError("selection_changed", "现有资源配置无法读取，保留原文件并拒绝覆盖。")
        current = safe_read(self.config, SUMMARY_LIMIT) if self.config.exists() else None
        if current != self.config_baseline:
            raise ResourceError("selection_changed", "资源配置已被外部修改，未覆盖；请重新打开核对。")
        self.refresh_reference_token()
        value = dict(version=SELECTION, revision=self.revision,
                     reference={k: self.selection[k] for k in ("path", "sha256")})
        try:
            atomic_json(self.config, value)
        except (OSError, CoreError) as exc:
            reason='没有写入资源配置目录的权限，请恢复本包目录权限后重试。' if isinstance(exc,PermissionError) else '资源引用保存未确认：'+str(exc)
            self.warning = translate_error(ResourceError("selection_save_failed", reason, retryable=True))
            raise ResourceError("selection_save_failed", self.warning["message"], retryable=True) from exc
        self.config_baseline = encoded(value); self.saved_selection = deepcopy(self.selection)
        self.dirty = False; self.warning = None
        return self.describe()

    def discard(self):
        self.revision += 1; self.generation += 1
        self.selection = deepcopy(self.saved_selection); self.dirty = False; self.last = None
        self.attempt = None; self.warning = None
        return self.describe()

    def owner(self, session, request, operation):
        if self.dirty:
            raise ResourceError("selection_unsaved", "请先保存资源引用，再开始检查。")
        if not self.selection:
            raise ResourceError("resource_missing", "请先选择并保存现有资源引用。")
        self.refresh_reference_token()
        if safe_read(self.config,SUMMARY_LIMIT)!=self.config_baseline:
            raise ResourceError('selection_changed','保存的配置或资源引用已被外部改变，请重新打开核对。')
        self.generation += 1
        return dict(worker_session=session, request_id=request, operation_id=operation,
                    selection_revision=self.revision, resource_set_id=self.selection["resource_set_id"],
                    generation=self.generation)

    def check(self, owner, cancel, progress, *, runtime=False, deadline=None):
        if self.dirty or self.config_blocked:
            raise ResourceError('selection_unsaved','请先保存合法的资源引用。')
        if not self.selection or (owner["selection_revision"], owner["resource_set_id"], owner["generation"]) != (
                self.revision, self.selection["resource_set_id"], self.generation):
            raise ResourceError("selection_changed", "检查已不属于当前资源选择。")
        start = time.monotonic(); deadline = min(deadline or start + CHECK_SECONDS, start + CHECK_SECONDS)
        self.attempt = dict(owner=owner, status="checking", checked_at=time.time())
        evidence = {}; total = 0
        try:
            planned_bytes=0
            # Reject an oversized aggregate before reading its first resource.
            # Missing/corrupt individual manifests are reported independently in
            # the normal per-resource pass below.
            for spec in self.selection['resources']:
                stop_check(cancel,deadline)
                try:
                    _,_,size=manifest_entries(spec)
                    planned_bytes+=size
                    if planned_bytes>SET_LIMIT:
                        raise ResourceError('resource_limit','资源集合声明的累计字节超过上限。')
                except (CoreError,OSError) as exc:
                    if getattr(exc,'code',None)=='resource_limit':raise
            for spec in self.selection["resources"]:
                stop_check(cancel, deadline)
                try:
                    result = verify_resource(spec, cancel, deadline, progress)
                    total += result["bytes"]
                    if total > SET_LIMIT:
                        raise ResourceError("resource_limit", "资源集合累计读取超过上限。")
                    evidence[spec["resource_id"]] = result
                except (CoreError, OSError) as exc:
                    if getattr(exc, "code", None) in ("check_cancelled", "check_timeout", "resource_limit"):
                        raise
                    error = translate_error(exc, spec["resource_id"])
                    state = {"resource_missing": "missing", "resource_corrupt": "corrupt",
                             "resource_incompatible": "incompatible", "resource_changed": "stale"}.get(error["code"], "failed")
                    evidence[spec["resource_id"]] = dict(state=state, error=error, identity=content_identity(spec))
            probe = None
            speech_verified = evidence.get('speech.runtime', {}).get('state') == 'files_verified'
            native_verified = evidence.get('mac.frameworks', {}).get('state') == 'files_verified'
            if runtime and self.probe and (speech_verified or native_verified):
                stop_check(cancel, deadline)
                # The Mac player can run with only the verified system helper.
                # Do not pass an unverified executable to the speech probe.
                selection = self.selection if speech_verified else dict(self.selection, resources=[
                    spec for spec in self.selection['resources'] if spec['resource_id'] != 'speech.runtime'])
                probe = self.probe(selection, self.root, cancel, deadline)
            stop_check(cancel, deadline)
            if self.dictionary:
                self.dictionary.refresh(cancel=cancel)
            stop_check(cancel, deadline)
            for spec in self.selection["resources"]:
                result = evidence[spec["resource_id"]]
                if result["state"] == "files_verified" and not tokens_match(spec, result):
                    result.update(state="stale", error=ResourceError("resource_changed", "检查期间资源被替换。", resource_id=spec["resource_id"]).wire())
            self.refresh_reference_token()
            receipt = dict(version=CHECK, summary=dict(owner=owner, checked_at=time.time(),
                elapsed_seconds=time.monotonic()-start, hash_bytes=total,
                runtime_requested=runtime, platform=sys.platform, machine=platform.machine(),
                os_release=platform.release(), probe=probe, verifier=verifier_identity(), model_load="not_tested", inference="not_tested"),
                evidence=evidence, dictionary=self.dictionary.snapshot() if self.dictionary else None)
            receipt_path = self.root / ("check-" + owner["operation_id"] + ".json")
            atomic_json(receipt_path, receipt)
            receipt["receipt"] = dict(path=receipt_path.name, bytes=receipt_path.stat().st_size,
                                      sha256=hashlib.sha256(safe_read(receipt_path, 32*1024*1024)).hexdigest())
            atomic_json(self.root / "last-check.json", receipt)
            self.last = receipt
            self.attempt = dict(owner=owner, status="completed", checked_at=time.time())
        except (CoreError, OSError) as exc:
            error = translate_error(exc)
            status = {"check_cancelled": "cancelled", "check_timeout": "timed_out"}.get(error["code"], "failed")
            self.attempt = dict(owner=owner, status=status, checked_at=time.time(), error=error,
                                elapsed_seconds=time.monotonic()-start)
            atomic_json(self.root / ("attempt-" + owner["operation_id"] + ".json"), self.attempt)
        return self.describe()

    def describe(self):
        current = self.selection; rows = []; states = {}
        config_matches=not self.config_blocked
        local_warning=self.warning
        try:
            disk=safe_read(self.config,SUMMARY_LIMIT) if self.config.exists() or self.config.is_symlink() else None
            config_matches=config_matches and disk==self.config_baseline
            self.refresh_reference_token()
        except (OSError,CoreError):config_matches=False
        if not config_matches and not local_warning:
            local_warning=ResourceError('selection_changed','配置或引用文件已改变，当前绑定失效。请保留原文件并重新打开核对。').wire()
        summary = self.last["summary"] if self.last else None
        fresh = bool(current and summary and summary["owner"]["resource_set_id"] == current["resource_set_id"] and
                     0 <= time.time() - summary["checked_at"] <= EVIDENCE_SECONDS and
                     summary["os_release"] == platform.release() and summary["machine"] == platform.machine() and
                     summary.get('verifier')==verifier_identity())
        if current:
            try:
                fresh = fresh and token(current["path"]) == current["token"]
            except OSError: fresh = False
            for spec in current["resources"]:
                evidence = self.last["evidence"].get(spec["resource_id"]) if self.last and fresh else None
                state = evidence["state"] if evidence else "unchecked"
                if evidence and state == "files_verified" and not tokens_match(spec, evidence):
                    state = "stale"
                states[spec["resource_id"]] = state
                rows.append(dict(resource_id=spec["resource_id"], role=spec["role"], version=spec["version"],
                    state=state, identity=content_identity(spec), root=spec["root"],
                    manifest=spec["manifest"], origin=spec["origin"], license=spec["license"],
                    distribution_status=spec["distribution_status"], ownership=spec["ownership"],
                    files=evidence.get("files") if evidence else None, bytes=evidence.get("bytes") if evidence else None,
                    error=evidence.get("error") if evidence else None))
        probe = summary.get("probe") if summary and fresh else None
        available = probe.get("capabilities", {}) if probe else {}
        if probe and not probe.get("current_boot", False):
            available = {}
        if probe:
            from .resource_macos import boot_identity
            boot=boot_identity()
            if (not boot or probe.get('boot_id')!=boot) and probe.get('worker_pid')!=os.getpid(): available = {}
        caps = []
        for capability, required in REQUIRED.items():
            missing = [r for r in required if states.get(r) != "files_verified"]
            measured = available.get(capability) is True
            caps.append(dict(capability=capability, resources=list(required), unsatisfied=missing,
                state="runtime_verified" if not missing and measured and not self.dirty and config_matches else
                      "files_verified" if not missing else "unchecked" if current else "unconfigured",
                prerequisite_satisfied=not missing and measured and not self.dirty and config_matches,
                runtime_probe="measured" if measured else "not_tested", model_load="not_tested", inference="not_tested"))
        value = dict(version=CONTRACT, selection_revision=self.revision, generation=self.generation,
            resource_set_id=current["resource_set_id"] if current else None,
            title=current["title"] if current else None, reference_path=current["path"] if current else None,
            saved=not self.dirty and current is not None and config_matches, dirty=self.dirty, warning=local_warning,
            resources=rows, capabilities=caps, last_check=summary,
            receipt=self.last.get("receipt") if self.last and fresh else None, attempt=self.attempt,
            inherited=self.inherited(), coreml=dict(state="not_selected", automatic_fallback=False))
        if len(encoded(value)) > 48000:
            raise ResourceError("resource_limit", "资源摘要超过传输上限。")
        return value

    def inherited(self):
        dictionary = self.dictionary.snapshot() if self.dictionary else None
        translation=dict(recipe=default_recipe("en-zh"),
            state="unchecked", endpoint_probed=False, meaning="仅保留既有翻译配置，主动服务检查另记")
        if self.service_evidence: translation=deepcopy(self.service_evidence)
        return dict(dictionary=dictionary,translation=translation,adapters=deepcopy(self.adapters))

    def restore_dictionary(self):
        if (self.dictionary and self.last and self.last.get("dictionary") and
                0 <= time.time()-self.last['summary']['checked_at'] <= EVIDENCE_SECONDS):
            self.dictionary.restore(self.last["dictionary"])

    def make_binding(self, capability, execution=None):
        if execution is None: execution = EXECUTION
        description = self.describe()
        selected = next((c for c in description["capabilities"] if c["capability"] == capability), None)
        if self.dirty:
            raise ResourceError("selection_unsaved", "资源引用尚未保存，不能绑定任务。")
        if self.selection and not description['saved']:
            raise ResourceError('selection_changed','已保存的配置或引用发生变化，不能绑定任务。')
        if not selected or not selected["prerequisite_satisfied"]:
            raise ResourceError("capability_unverified", "当前能力的资源前置尚未实测通过。", capability=capability)
        wanted = set(selected["resources"])
        resources = {r["resource_id"]: dict(identity=r["identity"], root=r["root"], manifest=r["manifest"])
                     for r in description["resources"] if r["resource_id"] in wanted}
        if not valid_speech_execution(capability,execution,resources):
            raise ResourceError("resource_incompatible", "所选 Whisper 模型不支持当前原文语言。")
        return encoded(dict(version=BINDING, capability=capability, execution=execution,
            resources=resources, prerequisite_only=True, model_load="not_tested", inference="not_tested",
            check=description["receipt"], checked_at=description["last_check"]["checked_at"]))


def parse_binding(raw):
    value = json_value(raw) if isinstance(raw, (str, bytes)) else deepcopy(raw)
    keys = {"version", "capability", "execution", "resources", "prerequisite_only", "model_load",
            "inference", "check", "checked_at"}
    speech_preset = isinstance(value, dict) and value.get("version") == SPEECH_BINDING
    if (not isinstance(value, dict) or set(value) != (keys | {"speech"} if speech_preset else keys)
            or value["version"] not in (BINDING, SPEECH_BINDING) or value["capability"] not in REQUIRED
            or not isinstance(value["resources"], dict)):
        raise ResourceError("resource_incompatible", "任务资源绑定格式不受支持。")
    required = REQUIRED[value["capability"]]
    if speech_preset:
        from .speech_presets import QWEN_BACKEND, QWEN_RUNTIME, validate_speech_metadata
        if (not isinstance(value["speech"], dict) or value["prerequisite_only"] is not False or
                value["model_load"] != "verified" or value["inference"] != "verified"):
            raise ResourceError("resource_incompatible", "语音预设绑定缺少能力验证。")
        validate_speech_metadata(value["speech"], value["capability"], value["resources"])
        if value["speech"]["backend"] == QWEN_BACKEND:
            required = ("speech.runtime", "speech.bridge", "speech.model", "speech.backend", "speech.qwen-runtime")
            valid_execution = value["execution"] == dict(language="en", backend=QWEN_BACKEND, runtime=QWEN_RUNTIME)
        else:
            valid_execution = valid_speech_execution(value["capability"], value["execution"], value["resources"])
    else:
        if (value["prerequisite_only"] is not True or value["model_load"] != "not_tested" or
                value["inference"] != "not_tested"):
            raise ResourceError("resource_incompatible", "既有能力绑定格式不受支持。")
        valid_execution = valid_speech_execution(value["capability"], value["execution"], value["resources"])
    if not valid_execution or set(value["resources"]) != set(required):
        raise ResourceError("resource_incompatible", "任务资源绑定格式不受支持。")
    for item in value["resources"].values():
        if not isinstance(item, dict) or set(item) != {"identity", "root", "manifest"}:
            raise ResourceError("resource_incompatible", "任务资源绑定缺少身份或位置。")
        hash_value(item["identity"]); bounded_string(item["root"], 4096)
        manifest=item['manifest']
        if (not isinstance(manifest,dict) or set(manifest)!={'path','bytes','sha256','format'} or
                type(manifest['bytes']) is not int or not 0<manifest['bytes']<=MANIFEST_LIMIT or
                manifest['format'] not in ('component-v1','model-v0','assets-v1','resource-files-v1') or
                not isinstance(manifest['path'],str) or not Path(manifest['path']).is_absolute() or
                '..' in Path(manifest['path']).parts or not Path(item['root']).is_absolute() or '..' in Path(item['root']).parts):
            raise ResourceError("resource_incompatible", "任务清单身份或位置无效。")
        hash_value(manifest['sha256'])
    if type(value['checked_at']) not in (int,float) or not math.isfinite(value['checked_at']):
        raise ResourceError('resource_incompatible','任务绑定检查时间无效。')
    receipt=value['check']
    if (not isinstance(receipt,dict) or set(receipt)!={'path','bytes','sha256'} or type(receipt['bytes']) is not int or
            not 0<receipt['bytes']<=32*1024*1024):
        raise ResourceError('resource_incompatible','任务绑定缺少检查收据。')
    relative_name(receipt['path']);hash_value(receipt['sha256'])
    return value


def valid_receipt(value, root):
    try:
        if (not isinstance(value,dict) or set(value)!={'version','summary','evidence','dictionary','receipt'} or
                value['version']!=CHECK or not isinstance(value['summary'],dict) or not isinstance(value['evidence'],dict) or
                not 1<=len(value['evidence'])<=16): return False
        summary=value['summary']
        if set(summary)!={'owner','checked_at','elapsed_seconds','hash_bytes','runtime_requested','platform','machine','os_release','probe','verifier','model_load','inference'}: return False
        if summary['verifier']!=verifier_identity():return False
        if (type(summary['checked_at']) not in (float,int) or summary['model_load']!='not_tested' or
                summary['inference']!='not_tested' or not isinstance(summary['owner'],dict) or
                set(summary['owner'])!={'worker_session','request_id','operation_id','selection_revision','resource_set_id','generation'}): return False
        if (not math.isfinite(summary['checked_at']) or type(summary['elapsed_seconds']) not in (int,float) or
                not math.isfinite(summary['elapsed_seconds']) or summary['elapsed_seconds']<0 or
                type(summary['hash_bytes']) is not int or not 0<=summary['hash_bytes']<=SET_LIMIT or
                type(summary['runtime_requested']) is not bool):return False
        if any(type(summary['owner'][key]) is not int or not 1<=summary['owner'][key]<2**53 for key in ('selection_revision','generation')):return False
        for name in ('worker_session','request_id','operation_id'):
            if str(uuid.UUID(summary['owner'][name]))!=summary['owner'][name]: return False
        hash_value(summary['owner']['resource_set_id'])
        probe=summary['probe']
        if probe is not None:
            if (not isinstance(probe,dict) or probe.get('version')!='mac-resource-probe-v1' or
                    not isinstance(probe.get('capabilities'),dict) or set(probe['capabilities'])!=set(REQUIRED) or
                    any(type(v) is not bool for v in probe['capabilities'].values()) or
                    probe.get('model_load')!='not_tested' or probe.get('inference')!='not_tested' or
                    type(probe.get('current_boot')) is not bool or not isinstance(probe.get('speech'),dict) or
                    not isinstance(probe.get('frameworks'),dict) or not isinstance(probe.get('gui'),dict)):return False
        if value['dictionary'] is not None and not isinstance(value['dictionary'],dict):return False
        for item in value['evidence'].values():
            if not isinstance(item,dict) or item.get('state') not in ('files_verified','missing','corrupt','incompatible','failed','stale'): return False
            hash_value(item['identity'])
            if item['state']=='files_verified':
                if (not isinstance(item.get('records'),dict) or not 1<=len(item['records'])<=FILE_COUNT or
                        len(item['manifest_token'])!=6 or len(item['root_token'])!=6): return False
                for name,record in item['records'].items():
                    relative_name(name)
                    if not isinstance(record,dict) or len(record['token'])!=6: return False
        receipt=value['receipt']
        if not isinstance(receipt,dict) or set(receipt)!={'path','bytes','sha256'}: return False
        relative_name(receipt['path'])
        if '/' in receipt['path'] or not receipt['path'].startswith('check-'): return False
        original=safe_read(root/receipt['path'],32*1024*1024)
        if len(original)!=receipt['bytes'] or hashlib.sha256(original).hexdigest()!=receipt['sha256']: return False
        return json_value(original)=={k:v for k,v in value.items() if k!='receipt'}
    except (CoreError,OSError,KeyError,TypeError,ValueError): return False


def compare_binding(saved, current):
    left, right = parse_binding(saved), parse_binding(current)
    if (left["capability"] != right["capability"] or left["execution"] != right["execution"] or
            (left.get("speech") and right.get("speech") and left["speech"] != right["speech"]) or
            {k:v["identity"] for k,v in left["resources"].items()} !=
            {k:v["identity"] for k,v in right["resources"].items()} or
            {k:{a:b for a,b in v['manifest'].items() if a!='path'} for k,v in left['resources'].items()} !=
            {k:{a:b for a,b in v['manifest'].items() if a!='path'} for k,v in right['resources'].items()}):
        return "incompatible"
    return "identical" if left["resources"] == right["resources"] else "relocated"


def validate_description(value):
    """Bounded complete public envelope; reject future contracts at the client."""
    keys = {'version','selection_revision','generation','resource_set_id','title','reference_path','saved','dirty',
            'warning','resources','capabilities','last_check','receipt','attempt','inherited','coreml'}
    if (not isinstance(value,dict) or set(value)!=keys or value['version']!=CONTRACT or
            any(type(value[k]) is not int or not 0<=value[k]<2**53 for k in ('selection_revision','generation')) or
            any(type(value[k]) is not bool for k in ('saved','dirty')) or
            not isinstance(value['resources'],list) or len(value['resources'])>16 or
            not isinstance(value['capabilities'],list) or len(value['capabilities'])!=len(REQUIRED) or
            not isinstance(value['inherited'],dict) or set(value['inherited'])!={'dictionary','translation','adapters'} or
            value['coreml']!=dict(state='not_selected',automatic_fallback=False) or len(encoded(value))>48000):
        raise ResourceError('resource_incompatible','资源摘要字段或接口版本不受支持。')
    if value['resource_set_id'] is not None: hash_value(value['resource_set_id'])
    ids=set()
    for row in value['resources']:
        if (not isinstance(row,dict) or set(row)!={'resource_id','role','version','state','identity','root','manifest',
                'origin','license','distribution_status','ownership','files','bytes','error'} or
                row['state'] not in ('unchecked','files_verified','stale','missing','corrupt','incompatible','failed') or
                row['resource_id'] in ids):
            raise ResourceError('resource_incompatible','单项资源状态无效。')
        ids.add(bounded_string(row['resource_id'])); hash_value(row['identity'])
    seen=set()
    for capability in value['capabilities']:
        if (not isinstance(capability,dict) or set(capability)!={'capability','resources','unsatisfied','state',
                'prerequisite_satisfied','runtime_probe','model_load','inference'} or
                capability['capability'] not in REQUIRED or capability['capability'] in seen or
                capability['resources']!=list(REQUIRED[capability['capability']]) or
                not isinstance(capability['unsatisfied'],list) or
                any(v not in capability['resources'] for v in capability['unsatisfied']) or
                type(capability['prerequisite_satisfied']) is not bool or
                capability['model_load']!='not_tested' or capability['inference']!='not_tested' or
                capability['state'] not in ('unconfigured','unchecked','files_verified','runtime_verified') or
                capability['runtime_probe'] not in ('measured','not_tested') or
                capability['prerequisite_satisfied'] and (capability['state']!='runtime_verified' or capability['unsatisfied'])):
            raise ResourceError('resource_incompatible','能力证据级别无效。')
        seen.add(capability['capability'])
    for name in ('last_check','receipt','attempt','warning'):
        if value[name] is not None and not isinstance(value[name],dict):
            raise ResourceError('resource_incompatible','资源证据摘要无效。')
    return value
