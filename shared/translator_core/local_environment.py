"""Read-only discovery and opt-in acquisition for the Mac package."""
import hashlib
import http.client
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import Cancelled, CoreError
from .ollama import Ollama, model_digest_matches, supported_mac_version
from .preferences import MODELS
from .resource_macos import DEFAULT_WHISPER, LEGACY_WHISPER, whisper_variant
from .resources import (CHECK_SECONDS, atomic_json, encoded, json_value, manifest_entries, no_links,
                        relative_name, safe_read, token, verify_resource)


DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
OLLAMA_DOWNLOAD = "https://ollama.com/download/mac"
OLLAMA_MODEL_DOWNLOADS = {
    "hy-mt2:1.8b-q8": "https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF/blob/a0c709d9fac510f2c807aa3af52872340dc37a4a/Hy-MT2-1.8B-Q8_0.gguf",
    "hy-mt2:7b-q8": "https://huggingface.co/tencent/Hy-MT2-7B-GGUF/blob/ab8472660ac61fac25f1af43fac2599d52a8a775/HY-MT2-7B-Q8_0.gguf",
}
WHISPER_COMMIT = "363e4f6d029694d9c81ae548beddd9d3c88a3637"
WHISPER_ARCHIVE_ROOT = "speech-runtime-0.2.10-r5"
WHISPER_ARCHIVE = WHISPER_ARCHIVE_ROOT + "-macos-arm64.zip"
SPEECH_FOLDERS = {"speech.model": f"whisper-{DEFAULT_WHISPER}", "speech.assets": "speech-assets"}
REGISTRY = "https://registry.ollama.ai/v2/library/hy-mt2"
TRANSLATION_DOWNLOADS = {"translation.1.8b": "hy-mt2:1.8b-q8", "translation.7b": "hy-mt2:7b-q8"}
MAX_MANIFEST = 32 * 1024


def model_manifest(root, model):
    tag = model.removeprefix("hy-mt2:")
    return root/"manifests/registry.ollama.ai/library/hy-mt2"/tag


def manifest_layers(raw, model):
    if hashlib.sha256(raw).hexdigest() != MODELS[model]:
        raise CoreError("上游模型版本与此 App 支持的版本不同。", "model_mismatch")
    manifest = json_value(raw)
    return [(layer["digest"], layer["size"]) for layer in (manifest["config"], *manifest["layers"])]


def regular_size(path, size):
    try:
        info = token(no_links(path))
        return stat.S_ISREG(info[2]) and info[3] == size
    except (OSError, CoreError):
        return False


def installed_ollama_model(root, model):
    try:
        layers = manifest_layers(safe_read(model_manifest(root, model), MAX_MANIFEST), model)
        return all(regular_size(root/"blobs"/digest.replace(":", "-"), size) for digest, size in layers)
    except (OSError, CoreError, KeyError, TypeError):
        return False


def speech_files_present(spec):
    if not spec:
        return False
    try:
        entries, _, _ = manifest_entries(spec)
        root = Path(spec["root"])
        return bool(entries) and all(regular_size(root/name, row["size"]) for name, row in entries.items())
    except (OSError, CoreError, KeyError, TypeError, ValueError):
        return False


def speech_target(data_root, resource_id, spec=None):
    folder = SPEECH_FOLDERS[resource_id]
    if resource_id == "speech.model" and spec:
        variant = whisper_variant(spec["manifest"]["sha256"])
        if variant:
            folder = f"whisper-{variant}"
    return data_root/"models"/folder


def whisper_runtime_target(data_root):
    return data_root/"models/whisper-runtime"


def whisper_runtime_present(spec):
    if not spec:
        return False
    from .resource_macos import TRUSTED_MANIFESTS
    try:
        root = Path(spec["root"])
        manifest = safe_read(root/"component.json")
        executable = no_links(root/"Python/bin/python3.12")
        return (hashlib.sha256(manifest).hexdigest() == TRUSTED_MANIFESTS["speech.runtime"]
                and executable.is_file() and os.access(executable, os.X_OK)
                and no_links(root/"Code/pinned-source.json").is_file())
    except (OSError, CoreError):
        return False


def legacy_whisper_runtime():
    environments = Path.home()/"Library/Application Support/LocalTranslator/Environments"
    try:
        active = json_value(safe_read(environments/"active.json", 8192))
        relative = relative_name(active["components"]["speech-runtime"])
        if active["schema"] == 1 and relative.startswith("versions/speech-runtime/"):
            return environments/relative
    except (OSError, CoreError, KeyError, TypeError, ValueError):
        pass
    return None


def external_speech_root(root):
    # Older App bundles shipped an install source. Use installed resources
    # independently of whether that old .app still exists.
    return not any(parent.name == 'install-sources' and parent.parent.name == 'Runtime'
                   for parent in Path(root).parents)


def discover_speech_folder(data_root, spec, beside_app=None):
    key = spec["resource_id"]
    local = speech_target(data_root, key, spec)
    model_library = Path.home()/"Library/Application Support/LocalTranslator/Models"
    candidates = [local, model_library/local.name]
    if key == "speech.model":
        variant = whisper_variant(spec["manifest"]["sha256"])
        if variant == LEGACY_WHISPER:
            candidates.extend((model_library/"whisper-mps-experiment/large-v3-turbo", model_library/"large-v3-turbo"))
        elif variant == DEFAULT_WHISPER:
            candidates.extend((model_library/"small.en", model_library/"whisper-small.en-fp16"))
    else:
        candidates.append(model_library/"speech-runtime-data-v1")
    if beside_app:
        names = [local.name] if key == 'speech.model' else [local.name, 'speech-runtime-data-v1']
        candidates.extend(base/name for base in (beside_app, beside_app/'models') for name in names)
    candidates.append(Path(spec['root']))
    return next((root for root in dict.fromkeys(candidates)
                 if external_speech_root(root) and speech_files_present(dict(spec, root=str(root)))), local)


def inspect_ollama(endpoint, cancel):
    from .translation_routes import local_tags
    client = Ollama(endpoint, cancel=cancel)
    version = client.json("/api/version", timeout=1.2).get("version")
    if not isinstance(version, str) or not version.strip() or len(version) > 96:
        raise CoreError("Ollama 版本响应无效。", "service_incompatible")
    if not supported_mac_version(version):
        return {"endpoint": endpoint, "version": version, "compatible": False, "models": [], "model_count": 0}
    tags = client.json("/api/tags", timeout=1.2).get("models")
    if not isinstance(tags, list):
        raise CoreError("Ollama 模型列表无效。", "service_incompatible")
    compatible = []
    for name, digest in MODELS.items():
        tag = next((row for row in tags if isinstance(row, dict) and row.get('name') == name
                    and all(row.get(field) is None for field in ('remote_host', 'remote_model'))), None)
        if tag is None:
            continue
        try:
            model = client.json("/api/show", {"model": name}, timeout=1.2)
            if (isinstance(model.get("capabilities"), list) and "completion" in model["capabilities"]
                    and all(model.get(field) is None for field in ("remote_host", "remote_model"))
                    and model_digest_matches(name, digest, tag.get('digest'), model, allow_mac_gguf=True)):
                compatible.append(name)
        except (CoreError, OSError, ValueError, TypeError, AttributeError):
            continue
    return {"endpoint": endpoint, "version": version, "compatible": True,
            "models": compatible, "catalog": list(local_tags(tags)), "model_count": len(tags)}


def ollama_installation():
    for app in (Path("/Applications/Ollama.app"), Path.home()/"Applications/Ollama.app"):
        binary = app/"Contents/Resources/ollama"
        if binary.is_file() and os.access(binary, os.X_OK):
            try:
                version = plistlib.loads(safe_read(app/"Contents/Info.plist", MAX_MANIFEST)).get("CFBundleShortVersionString")
            except (OSError, CoreError, ValueError, plistlib.InvalidFileException):
                version = None
            return {"installed": True, "version": str(version)[:96] if version else None}
    binary = mac_utility("ollama")
    return {"installed": binary, "version": None}


def mac_utility(name):
    return bool(shutil.which(name) or any(
        (folder/name).is_file() and os.access(folder/name, os.X_OK)
        for folder in (Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path.home()/".local/bin")
    ))


def ollama_addresses(endpoint, explicit=False):
    if explicit:
        return (endpoint,)
    current = endpoint.rstrip('/')
    return tuple(dict.fromkeys((current, DEFAULT_ENDPOINT))) if current != 'http://127.0.0.1:1' else (DEFAULT_ENDPOINT,)


def external_ollama_route(services, preferred_model, *, exact=False):
    from .translation_routes import AUTO_MODEL
    for service in services:
        models = service.get('catalog', service['models'])
        model = next(iter(models),None) if preferred_model==AUTO_MODEL else preferred_model if preferred_model in models else None
        if not model and not exact:
            model=next(iter(service['models']),None)
        if model:
            return dict(endpoint=service['endpoint'], version=service['version'], model=model)
    return None


def discover_ollama(addresses, cancel):
    external = []
    for address in addresses:
        if cancel.is_set():
            raise Cancelled()
        try:
            external.append(inspect_ollama(address, cancel))
        except (CoreError, OSError, ValueError, TypeError, AttributeError):
            continue
    return external


def environment_status(addresses, specs, runtime_verified, cancel):
    external = discover_ollama(addresses, cancel)
    current_specs = {item["resource_id"]: item for item in specs}
    whisper = current_specs.get("speech.runtime")
    runtime = bool(whisper and external_speech_root(whisper['root']) and whisper_runtime_present(whisper))
    speech = {key: bool(current_specs.get(key) and external_speech_root(current_specs[key]['root'])
                        and speech_files_present(current_specs[key])) for key in SPEECH_FOLDERS}
    model = current_specs.get("speech.model")
    speech.update(runtime=runtime, verified=runtime_verified and runtime and all(speech.values()),
                  model_variant=whisper_variant(model["manifest"]["sha256"]) if model else None,
                  standalone=[name for name in ("whisper", "whisper-cli", "whisperlivekit-server", "mlx_whisper")
                              if mac_utility(name)])
    return {"ollama": {"installation": ollama_installation(), "external": external}, "whisper": speech}


def dictionary_catalog(config, task_root, cancel):
    from .dictionary.resources import ResourceCatalog
    catalog = ResourceCatalog(config, forbidden_roots=(task_root,), lazy=True)
    if not catalog.resources or catalog.config_error:
        raise CoreError("请选择有效的词典 resources.json 文件。", "dictionary_incompatible")
    # Apply the ordinary resource bounds before opening an arbitrary local
    # dictionary and use the same checksum and SQLite validation as lookup.
    for spec in catalog.resources.values():
        for role in ("source", "index"):
            size = spec.get(role+"_bytes")
            if type(size) is not int or not 0 < size <= 1024**3 or not regular_size(Path(spec[role+"_path"]), size):
                raise CoreError("词典文件缺失、大小不符或路径不可读。", "dictionary_incompatible")
    catalog.refresh(cancel)
    if not all(catalog.state(direction) == "ready" for direction in catalog.resources):
        raise CoreError("词典校验失败，请检查来源文件和索引版本。", "dictionary_incompatible")
    return catalog


def publish_import(payload, target, cancel):
    """Publish a complete copy without displacing an earlier resource."""
    if cancel.is_set():
        raise Cancelled()
    target = no_links(target)
    if target.exists():
        target = target.with_name(target.name+'-import-'+uuid.uuid4().hex)
    payload.rename(target)
    return target


def import_dictionary(config, data_root, task_root, selected_config, cancel, progress):
    from .dictionary.contracts import RESOURCE_VERSION
    from .resource_lifecycle import copy_verified_file
    source = dictionary_catalog(config, task_root, cancel)
    files = []
    document = dict(version=RESOURCE_VERSION, resources=[])
    for direction, spec in sorted(source.resources.items()):
        local = dict(spec)
        for role in ('source', 'index'):
            field = role+'_path'
            relative = Path(direction)/('index.sqlite' if role == 'index' else 'source')
            expected = dict(size=spec[role+'_bytes'], sha256=spec[role+'_sha256'])
            files.append((Path(spec[field]), relative, expected))
            local[field] = relative.as_posix()
        document['resources'].append(local)
    raw = encoded(document)
    library = no_links(data_root/'dictionaries')
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = library/hashlib.sha256(raw).hexdigest()
    for candidate in dict.fromkeys((selected_config, target/'resources.json')):
        if not candidate or not candidate.is_relative_to(library):
            continue
        try:
            if safe_read(candidate, MAX_MANIFEST) == raw:
                return dictionary_catalog(candidate, task_root, cancel)
        except (OSError, CoreError):
            if cancel.is_set():
                raise Cancelled() from None
    total = sum(expected['size'] for _, _, expected in files)
    if shutil.disk_usage(library).free < total + len(raw) + 64 * 1024**2:
        raise CoreError('磁盘空间不足，无法导入词典。', 'insufficient_space')
    deadline = time.monotonic() + CHECK_SECONDS
    with tempfile.TemporaryDirectory(prefix='.dictionary-import-', dir=library) as work:
        payload = Path(work)/'payload'
        payload.mkdir(mode=0o700)
        copied = 0
        for path, relative, expected in files:
            copy_verified_file(path, payload/relative, expected, cancel, deadline,
                               lambda count: progress(copied+count, total))
            copied += expected['size']
        atomic_json(payload/'resources.json', document)
        installed = publish_import(payload, target, cancel)
    return dictionary_catalog(installed/'resources.json', task_root, cancel)


def import_speech_folder(folder, spec, data_root, selected_root, cancel, progress):
    from .resource_lifecycle import copy_manifest_resource
    resource_id = spec['resource_id']
    runtime = resource_id == 'speech.runtime'
    model = resource_id == 'speech.model'

    def at(root):
        manifest = dict(spec['manifest'], path=str(root/'component.json')) if runtime else spec['manifest']
        return dict(spec, root=str(root), manifest=manifest)

    source = no_links(folder)
    target = whisper_runtime_target(data_root) if runtime else speech_target(data_root, resource_id, spec)
    deadline = time.monotonic() + CHECK_SECONDS
    library = no_links(target.parent)
    if not model:
        verify_resource(at(source), cancel, deadline, lambda *_: None)
        if source.is_relative_to(library):
            return source
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.speech-import-', dir=library) as work:
        payload = Path(work)/'payload'

        def stage():
            copy_manifest_resource(at(source), payload, cancel, deadline,
                                   lambda _, __, copied, total: progress(copied, total))
            verify_resource(at(payload), cancel, deadline, lambda *_: None)

        if model:
            # Downloaded model folders may also contain a model card or license.
            # Verify the declared weights and config in isolation before reusing any copy.
            stage()
        for root in dict.fromkeys((source, selected_root, target)):
            if not root or not root.is_relative_to(library) or not root.is_dir():
                continue
            try:
                verify_resource(at(root), cancel, deadline, lambda *_: None)
            except (OSError, CoreError) as exc:
                if getattr(exc, 'code', None) in ('check_cancelled', 'check_timeout'):
                    raise
            else:
                return root
        if not model:
            stage()
        return publish_import(payload, target, cancel)


def import_speech_tokenizer(file, assets, bundled_assets, data_root, cancel, progress):
    """Install one vetted vocabulary into a complete, independently verified asset copy."""
    from .resource_lifecycle import copy_verified_file

    file = no_links(file)
    if file.name not in ('gpt2.tiktoken', 'multilingual.tiktoken'):
        raise CoreError('请选择 Whisper 的 gpt2.tiktoken 或 multilingual.tiktoken。', 'tokenizer_format')
    entries, _, total = manifest_entries(bundled_assets)
    matched = [entry for name,entry in entries.items() if Path(name).name == file.name]
    if len(matched)!=2 or matched[0]!=matched[1] or not regular_size(file, matched[0]['size']):
        raise CoreError('分词器文件与此版本的 Whisper 不兼容。请选择匹配的 .tiktoken 文件。','tokenizer_format')
    deadline = time.monotonic() + CHECK_SECONDS
    base = assets or bundled_assets
    try:
        verify_resource(base, cancel, deadline, lambda *_: None)
    except (OSError, CoreError):
        if cancel.is_set():
            raise Cancelled() from None
        base = bundled_assets
    directory = no_links(Path(base['root']))
    target = speech_target(data_root, 'speech.assets', bundled_assets)
    library = no_links(target.parent)
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.tokenizer-import-', dir=library) as work:
        payload = Path(work)/'assets'
        payload.mkdir()
        copied = 0
        for name, entry in entries.items():
            source = file if Path(name).name==file.name else directory/name
            copy_verified_file(source, payload/name, entry, cancel, deadline,
                               lambda count: progress(copied+count,total))
            copied += entry['size']
        verify_resource(dict(bundled_assets,root=str(payload)), cancel, deadline, lambda *_: None)
        return publish_import(payload,target,cancel)


def trusted_download(url):
    try:
        value = urlsplit(url)
        host = value.hostname or ""
        return (value.scheme == "https" and not value.username and not value.password and value.port in (None, 443)
                and (host in ("registry.ollama.ai", "huggingface.co", "hf.co", "raw.githubusercontent.com",
                              "media.githubusercontent.com")
                     or host.endswith((".r2.cloudflarestorage.com", ".huggingface.co", ".hf.co"))))
    except ValueError:
        return False


class DownloadRedirect(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_download(newurl):
            raise CoreError("下载重定向到了未登记的地址。", "download_source")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_file(url, path, cancel, *, limit, sha256, exact, progress=lambda _: None, deadline=None):
    if cancel.is_set():
        raise Cancelled()
    if not trusted_download(url):
        raise CoreError("下载来源未登记。", "download_source")
    deadline = deadline if deadline is not None else time.monotonic() + 7200
    digest = hashlib.sha256()
    count = 0
    request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "LocalTranslator/0.2.11",
                                    "Accept": "application/vnd.docker.distribution.manifest.v2+json, */*"})
    try:
        with build_opener(DownloadRedirect()).open(request, timeout=12) as response, path.open("xb") as output:
            declared = response.headers.get("Content-Length")
            if declared is not None and (not declared.isdecimal() or int(declared) > limit or exact and int(declared) != limit):
                raise CoreError("服务器给出的文件大小与固定版本不同。", "download_integrity")
            while True:
                if cancel.is_set():
                    raise Cancelled()
                if time.monotonic() > deadline:
                    raise CoreError("下载超时，请重试。", "download_timeout")
                block = response.read1(min(1024**2, limit-count+1))
                if not block:
                    break
                count += len(block)
                if count > limit:
                    raise CoreError("服务器返回的文件超过登记大小。", "download_integrity")
                digest.update(block)
                output.write(block)
                progress(count)
            if digest.hexdigest() != sha256 or exact and count != limit:
                raise CoreError("下载文件与固定版本的大小或校验值不符。", "download_integrity")
            output.flush()
            os.fsync(output.fileno())
    except (OSError, http.client.HTTPException) as exc:
        if cancel.is_set():
            raise Cancelled() from exc
        raise CoreError("下载未完成，请检查网络或磁盘空间后重试。", "download_io") from exc


def publish_directory(payload, target, cancel):
    if cancel.is_set():
        raise Cancelled()
    backup = payload.parent/"previous"
    if target.exists():
        target.rename(backup)
    try:
        payload.rename(target)
    except OSError:
        if backup.exists():
            backup.rename(target)
        raise


def import_whisper_runtime(archive, signed_catalog, spec, data_root, cancel):
    catalog = json_value(safe_read(signed_catalog, MAX_MANIFEST))
    component = next(row for row in catalog["components"] if row["id"] == "speech-runtime")
    archive = no_links(archive)
    target = no_links(whisper_runtime_target(data_root))
    target.parent.mkdir(parents=True, exist_ok=True)
    if not regular_size(archive, component["archiveBytes"]):
        raise CoreError("请选择 Mac 兼容语音运行包 0.2.10-r5。", "runtime_archive")
    if shutil.disk_usage(target.parent).free < component["archiveBytes"]+component["installedBytes"]+64*1024**2:
        raise CoreError("磁盘空间不足，无法导入运行环境。", "download_space")
    with tempfile.TemporaryDirectory(prefix=".whisper-runtime-", dir=target.parent) as temp:
        stage = Path(temp)
        frozen = stage/"verified.zip"
        digest = hashlib.sha256()
        count = 0
        with archive.open("rb") as source, frozen.open("xb") as destination:
            while block := source.read(1024**2):
                if cancel.is_set():
                    raise Cancelled()
                count += len(block)
                if count > component["archiveBytes"]:
                    raise CoreError("运行包大小改变。", "runtime_archive")
                digest.update(block)
                destination.write(block)
        if count != component["archiveBytes"] or digest.hexdigest() != component["archiveSHA256"]:
            raise CoreError("语音运行包校验失败。", "runtime_archive")
        unpacked = stage/"unpacked"
        with subprocess.Popen(["/usr/bin/ditto", "-x", "-k", str(frozen), str(unpacked)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) as process:
            deadline = time.monotonic()+150
            while process.poll() is None:
                if cancel.wait(.1) or time.monotonic() >= deadline:
                    process.kill()
                    if cancel.is_set():
                        raise Cancelled()
                    raise CoreError("解包超时。", "runtime_archive")
            if process.returncode:
                raise CoreError("无法解开语音运行包。", "runtime_archive")
        payload = unpacked/relative_name(component["archiveRoot"])
        new_manifest = dict(spec["manifest"], path=str(payload/"component.json"))
        verify_resource(dict(spec, root=str(payload), manifest=new_manifest), cancel, time.monotonic()+120, lambda *_: None)
        # Give the fixed local installer the same commit boundary as downloads.
        final_stage = stage/"payload"
        payload.rename(final_stage)
        return publish_import(final_stage, target, cancel)


def fetch_speech(spec, target, cancel, progress):
    deadline = time.monotonic() + 7200
    manifest_path = Path(spec["manifest"]["path"])
    # Manifest identity is that of the signed app, even when the original
    # resource directory is absent.
    raw = safe_read(manifest_path)
    if hashlib.sha256(raw).hexdigest() != spec["manifest"]["sha256"]:
        raise CoreError("语音资源清单与应用版本不符。", "download_integrity")
    manifest = json_value(raw)
    files = manifest["files"]
    total = sum(entry["size"] for entry in files.values())
    target = no_links(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(target.parent).free < total + 64*1024**2:
        raise CoreError("磁盘空间不足，无法保存语音资源。", "download_space")
    with tempfile.TemporaryDirectory(prefix=".speech-", dir=target.parent) as temp:
        stage = Path(temp)
        payload = stage/"payload"
        payload.mkdir()
        count = 0
        for name, entry in files.items():
            if cancel.is_set():
                raise Cancelled()
            path = payload/relative_name(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if spec["resource_id"] == "speech.model":
                url = f"https://huggingface.co/{manifest['repo']}/resolve/{manifest['revision']}/{quote(name)}"
            else:
                if name.startswith("mlx/assets/"):
                    original_name = "wlk/whisper/assets/"+path.name
                    original = payload/original_name
                    if files.get(original_name) == entry and original.is_file():
                        shutil.copyfile(original, path)
                        count += entry["size"]
                        continue
                suffix = name.removeprefix("wlk/") if name.startswith("wlk/") else "whisper/assets/"+path.name
                url = f"https://raw.githubusercontent.com/QuentinFuxa/WhisperLiveKit/{WHISPER_COMMIT}/whisperlivekit/{quote(suffix)}"
            download_file(url, path, cancel, limit=entry["size"], sha256=entry["sha256"], exact=True, deadline=deadline,
                          progress=lambda received: progress(count+received, total))
            count += entry["size"]
        publish_directory(payload, target, cancel)
    progress(total, total)


def fetch_translation(root, model, cancel, progress):
    deadline = time.monotonic() + 7200
    root = no_links(root)
    root.mkdir(parents=True, exist_ok=True)
    tag = model.removeprefix("hy-mt2:")
    with tempfile.TemporaryDirectory(prefix=".ollama-", dir=root) as temp:
        stage = Path(temp)
        manifest = stage/"manifest"
        download_file(f"{REGISTRY}/manifests/{tag}", manifest, cancel, limit=MAX_MANIFEST, deadline=deadline,
                      sha256=MODELS[model], exact=False)
        layers = manifest_layers(manifest.read_bytes(), model)
        total = sum(size for _, size in layers)
        if shutil.disk_usage(root).free < total + 64*1024**2:
            raise CoreError("磁盘空间不足，无法保存翻译模型。", "download_space")
        count = 0
        for digest, size in layers:
            name = digest.replace(":", "-")
            path = stage/name
            download_file(f"{REGISTRY}/blobs/{digest}", path, cancel, limit=size, sha256=digest[7:], exact=True,
                          deadline=deadline,
                          progress=lambda received: progress(count+received, total))
            count += size
        if cancel.is_set():
            raise Cancelled()
        for digest, _ in layers:
            name = digest.replace(":", "-")
            target = no_links(root/"blobs"/name)
            target.parent.mkdir(parents=True, exist_ok=True)
            (stage/name).replace(target)
        target = no_links(model_manifest(root, model))
        target.parent.mkdir(parents=True, exist_ok=True)
        # The registry manifest becomes visible only after all blobs exist.
        manifest.replace(target)
    progress(total, total)
