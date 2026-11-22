"""Mac facts isolated in short-lived, explicitly owned offline processes."""
import os
import hashlib
import platform
import subprocess
import sys
import threading
import time
import uuid
from functools import cache
from pathlib import Path

from .resources import ResourceError, atomic_json, json_value, safe_read, stop_check, token
from .mac_sandbox import sandbox_command, release_temporary

LOG_LIMIT = 65536
# Execution trust is independent of user-selectable reference JSON. Runtime pin
# comes from the preserved build46 component catalog; helper/data/code pins are
# the reviewed T09-A registration's existing local reference. Relocation is OK.
TRUSTED_MANIFESTS={
    'speech.runtime':'8f343c8bebf5ab0d3362638a6e51672e31e22e6c2c6fee7a6acb281143e2d3b5',
    'speech.backend':'0098cfe0d97f1c7d57df4d10144830cf87212b5541b7227e49eab9c8e208aa8f',
    'speech.model':'038e96d7694ded8e2e34cbdf120ec825fa9d3b5dabbaf371551bce18b41eeea5',
    'speech.assets':'ae5a37caae1955a51e8df27f8561d12553034f2460f03e02f50deb7077baeb52',
    'speech.bridge':'2dae7c3a2c6ed95f80fc555e0109e4be895e4f499233d635d9725225349c1897',
}
TRUSTED_NATIVE='e97333039ec76791f9289158f377976922605b119cd337db960c94aad5084de4'
DEFAULT_WHISPER = 'small.en'
LEGACY_WHISPER = 'large-v3-turbo'
SMALL_MODEL_MANIFEST = '7a3b2071c7f00d099f20fd1a1216456e2f2fc8eb5acc08f9c1407ac4303aca0e'
MODEL_AWARE_BRIDGE = 'c6f5119c2b949c0439cce6002b0ec766df444cb2dc54b85905c51b47a665b9c8'
MULTILINGUAL_BRIDGE = 'deb996f94e05aa912ee9443931fe621fe9ff9906964a1167403c50c93d2f2d14'


def whisper_variant(digest):
    return {SMALL_MODEL_MANIFEST: DEFAULT_WHISPER, TRUSTED_MANIFESTS['speech.model']: LEGACY_WHISPER}.get(digest)


def trusted_manifest(name, digest):
    if name == 'speech.model':
        return whisper_variant(digest) is not None
    if name == 'speech.bridge':
        return digest in (TRUSTED_MANIFESTS[name], MODEL_AWARE_BRIDGE, MULTILINGUAL_BRIDGE)
    return digest == TRUSTED_MANIFESTS.get(name, digest)


def supported_speech_route(model_digest, bridge_digest):
    variant = whisper_variant(model_digest)
    return variant is not None and trusted_manifest('speech.bridge', bridge_digest) and (
        variant == LEGACY_WHISPER or bridge_digest in (MODEL_AWARE_BRIDGE, MULTILINGUAL_BRIDGE))


@cache
def boot_identity():
    # CPython exposes CLOCK_MONOTONIC since boot; one-second rounding is not
    # stable across calls, so use the kernel's read-only boot-time fact on Mac.
    if sys.platform != "darwin": return None
    try:
        return subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                                       timeout=2, text=True,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.SubprocessError):
        return None


def environment(cache):
    env = {k:v for k,v in os.environ.items() if k in ('LANG','LC_ALL','LC_CTYPE')}
    env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", TMPDIR=str(cache),
        TMP=str(cache), TEMP=str(cache), XDG_CACHE_HOME=str(cache), HF_HOME=str(cache / "hf"),
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
        NUMBA_CACHE_DIR=str(cache / "numba"), MPLCONFIGDIR=str(cache / "mpl"),
        TORCH_HOME=str(cache / "torch"), MTL_SHADER_CACHE_DIR=str(cache / "metal"),
        PYTORCH_ENABLE_MPS_FALLBACK="0", TOKENIZERS_PARALLELISM="false")
    return env


def run_owned(command, folder, cancel, deadline):
    folder.mkdir(parents=True, exist_ok=False)
    cache = folder / "cache"; cache.mkdir()
    # Reviewed entry points have no network fallback; OS policy also prevents
    # native libraries from writing old environments or using the network.
    escaped = str(folder).replace("\\", "\\\\").replace('"', '\\"')
    profile = '(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath "' + escaped + '") (literal "/dev/null"))'
    args = sandbox_command(profile, command, folder)
    child = subprocess.Popen(args, cwd=folder, env=environment(cache), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    atomic_json(folder / "owner.json", dict(pid=child.pid, parent_pid=os.getpid(), command=args,
        at=time.time(), models_loaded=False, inference=False, timeout_seconds=60))
    output, error = bytearray(), bytearray(); overflow = threading.Event()
    def read(pipe, buffer):
        with pipe:
            while data := pipe.read(4096):
                left = LOG_LIMIT - len(buffer)
                buffer.extend(data[:max(0, left)])
                if len(data) > left: overflow.set()
    readers = [threading.Thread(target=read, args=p) for p in ((child.stdout, output), (child.stderr, error))]
    for reader in readers: reader.start()
    began = time.monotonic(); forced = False; failure = None
    try:
        while child.poll() is None:
            stop_check(cancel, min(deadline, began + 60))
            if overflow.is_set(): raise ResourceError("resource_limit", "运行时检查日志超过上限。")
            cancel.wait(.03)
    except BaseException as exc:
        failure = exc
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); forced = True; child.wait(timeout=5)
    finally:
        child.wait(timeout=5)
        for reader in readers: reader.join(timeout=5)
        temporary_released = release_temporary(folder, child.pid)
        (folder / "stdout").write_bytes(output); (folder / "stderr").write_bytes(error)
        atomic_json(folder / "terminal.json", dict(pid=child.pid, OS_exit=child.returncode,
            elapsed_seconds=time.monotonic()-began, forced=forced,
            readers_drained=not any(r.is_alive() for r in readers), log_truncated=overflow.is_set(),
            temporary_released=temporary_released))
    if failure: raise failure
    if child.returncode or overflow.is_set():
        raise ResourceError("probe_failed", "无模型运行时检查未通过；请查看详细证据。", retryable=True,
                            next_action="保留现有任务，查看检查日志并恢复匹配的现有环境后重验。")
    return json_value(bytes(output))


def probe_resources(selection, root, cancel, deadline):
    if sys.platform != "darwin":
        raise ResourceError("resource_incompatible", "当前仅实现Mac资源实测适配。")
    specs = {s["resource_id"]: s for s in selection["resources"]}
    runtime = specs.get('speech.runtime')
    operation = root / ("probe-" + uuid.uuid4().hex); operation.mkdir()

    def attempt(command,folder):
        try:return run_owned(command,folder,cancel,deadline)
        except ResourceError as exc:
            if exc.code!='probe_failed':raise
            return dict(error=exc.wire())

    speech = dict(error='没有经过文件校验的语音运行环境。')
    if runtime:
        if (runtime['manifest']['sha256'] == TRUSTED_MANIFESTS['speech.runtime'] and
                runtime['manifest']['format'] == 'component-v1' and
                Path(runtime['manifest']['path']) == Path(runtime['root'])/'component.json'):
            manifest = json_value(safe_read(runtime['manifest']['path']))
            request = dict(runtime=runtime['root'], dependencies=manifest['dependencies'],
                           model_roots=[s['root'] for k, s in specs.items() if k in ('speech.model', 'speech.assets')])
            request_path = operation/'request.json'
            atomic_json(request_path, request)
            python = Path(runtime['root'])/'Python/bin/python3.12'
            before = token(python)
            script = Path(__file__).with_name('resource_macos_probe.py')
            speech = attempt([python, '-I', '-B', script, request_path], operation/'speech')
            if before != token(python):
                raise ResourceError('resource_changed', '检查期间解释器发生变化。')
        else:
            speech = dict(error='语音解释器与已审核的运行包不匹配。')
    frameworks = specs.get("mac.frameworks")
    helper=Path(frameworks['root'])/'resource-facts' if frameworks else None
    native = attempt([helper], operation / "frameworks") if helper and hashlib.sha256(safe_read(helper)).hexdigest()==TRUSTED_NATIVE else dict(error='Untrusted or missing native helper; not executed')
    gui_request=operation/'gui-request.json';atomic_json(gui_request,dict(prefix=sys.prefix))
    gui=attempt([sys.executable,'-I','-B',Path(__file__).with_name('resource_gui_probe.py'),gui_request],operation/'gui')
    valid_speech=(speech.get('version')=='mac-speech-import-probe-v1' and speech.get('models_loaded') is False and
                  speech.get('inference') is False and speech.get('audio_opened') is False and
                  speech.get('machine')=='arm64' and speech.get('python')=='3.12.14' and
                  speech.get('prefix')==str(Path(runtime['root'])/'Python'))
    errors = speech.get("errors", {'probe':'failed'}); origins = speech.get("origins", {}) if valid_speech else {}
    if not isinstance(errors,dict) or not isinstance(origins,dict):
        raise ResourceError('probe_failed','运行时检查响应格式不兼容。')
    for name,origin in origins.items():
        allowed=Path(runtime['root'])/('Code/source' if name=='whisperlivekit.core' else 'Python/lib/python3.12/site-packages')
        if not Path(origin).is_relative_to(allowed):
            raise ResourceError('probe_failed','运行时模块来源不属于固定解释器或封存后端。')
    common = all(n in origins and n not in errors for n in ("torch", "mlx.core", "mlx_whisper", "whisperlivekit.core", "tiktoken", "onnxruntime", "av", "soundfile"))
    pins = {name: specs.get(name, {}).get('manifest', {}).get('sha256') for name in TRUSTED_MANIFESTS}
    fixed_route = (runtime is not None and all(trusted_manifest(name, pin) for name, pin in pins.items())
                   and supported_speech_route(pins['speech.model'], pins['speech.bridge'])
                   and specs.get('speech.backend', {}).get('root') == runtime['root'])
    asr = fixed_route and common and speech.get("mps") is True and speech.get("mlx_gpu") is True
    refine = fixed_route and all(n in origins and n not in errors for n in ("mlx.core", "mlx_whisper", "soundfile", "tiktoken")) and speech.get("mlx_gpu") is True
    native_ok=(native.get('version')=='mac-resource-facts-v1' and native.get('avfoundation') is True and
               all(native.get(k) is False for k in ('audio_opened','playback','capture','model_load')))
    return dict(version="mac-resource-probe-v1", speech=speech, frameworks=native,gui=gui,
        capabilities={"speech.file_asr":asr, "speech.continuous_asr":asr, "speech.refine":refine,
                      "audio.decode":"av" in origins and trusted_manifest('speech.bridge', pins['speech.bridge']), "audio.playback":native_ok},
        boot_id=boot_identity(), worker_pid=os.getpid(), current_boot=True, evidence_directory=operation.name,fixed_route=fixed_route,
        model_load="not_tested", inference="not_tested", os_release=platform.release())
