"""Shared text worker and model-free lifecycle probe; no GPU framework imports."""
import argparse
import base64
import hashlib
import json
import os
import platform
import sys
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from translator_core import CORE_VERSION, Cancelled, CoreError
from translator_core.contracts import default_recipe, sha
from translator_core.ollama import Ollama
from translator_core.planning import platform_boundaries
from translator_core.store import TaskStore, atomic_write
from translator_core.capture_tasks import CaptureTasks
from translator_core.capture_macos import MacCapture
from translator_core.audio_macos import MacAudio
from translator_core.image_adapter import platform_image_adapter
from translator_core.pdf_adapter import platform_pdf_adapter
from translator_core.ocr_adapter import platform_ocr_adapter
from translator_core.text_policy import policy_identity
from .runtime import owned_python
from .dictionary_worker import DictionaryWorker
from .history_worker import HistoryWorker
from translator_core.history import QUERY_VERSION, METADATA_VERSION, LOCATOR_VERSION
from translator_core.resources import ResourceManager, ResourceError, CONTRACT as RESOURCE_CONTRACT, no_links, translate_error
from translator_core.resource_macos import probe_resources
from translator_core.preferences import Preferences, CONTRACT as PREFERENCES_CONTRACT, validate_execution, recipe_for
from translator_core.resource_lifecycle import ManagedResources, CONTRACT as MANAGED_CONTRACT
from translator_core.resource_service import TranslationService

from . import PREVIEW_ID, PRODUCT_VERSION, DEFAULT_ENDPOINT
from .paths import (LOCAL_DELIVERY, data_directory, initialize_package_resources,
                    package_resource_defaults, package_speech_variants, restore_bundled_speech,
                    select_speech_root, speech_origin)
from .speech_library import SpeechLibrary
from .protocol import (PROTOCOL, MAX_FRAME_BYTES, MAX_STEPS, SNAPSHOT_CHUNK_BYTES, STARTUP_ERROR_PREFIX, ProtocolError, WORKBENCH_OPERATIONS,
                       decode, encode, identifier)


def capabilities(configured=False, pdf_ready=False, ocr_state='requires_verified_ocr_adapter'):
    return {"diagnostics": "ready", "lifecycle_probe": "ready",
            "dictionary": "dictionary-query-v1", "resources": RESOURCE_CONTRACT,
            "history": QUERY_VERSION, "history_metadata": METADATA_VERSION, "history_locator": LOCATOR_VERSION,
            "text_translation": "ready" if configured and sys.platform in ("darwin", "win32") else "requires_verified_platform_adapter",
            "task_read_resume": "ready" if configured else "requires_preview_root",
            "pdf_txt": "ready" if configured and pdf_ready else "txt_ready_pdf_adapter_missing" if configured else "requires_preview_root", "ocr": ocr_state,
            "speech_file": "audio-task-v1", "refinement_playback": "audio-task-v1",
            "microphone": "audio-recording-v1", "system_audio": "audio-recording-v1", "service_unload": "not_implemented_b08",
            "runtime_installer": MANAGED_CONTRACT, "preferences": PREFERENCES_CONTRACT, "public_download": "not_implemented_b11"}


def diagnostics(configured=False, pdf_ready=False, ocr_state='requires_verified_ocr_adapter'):
    return {"preview": PREVIEW_ID, "product_version": PRODUCT_VERSION,
            "platform": sys.platform, "machine": platform.machine(),
            "os_release": platform.release(), "python": platform.python_version(),
            "worker_pid": os.getpid(), "isolated_python": bool(sys.flags.isolated),
            "bytecode_disabled": bool(sys.dont_write_bytecode),
            "transport": "anonymous-pipes-jsonl", "models_loaded": False,
            "production_tasks_opened": False, "gpu_verified": False,
            "gpu_verification_scope": "worker imports no GPU framework; actual helper evidence is in resources",
            "shared_core": CORE_VERSION, "inference_on_open": False,
            "capabilities": capabilities(configured, pdf_ready, ocr_state)}


class Worker:
    def __init__(self, session_id, output, task_root=None, boundary_helper=None, endpoint=None, pdf_helper=None, pdf_library=None, dictionary_config=None, image_helper=None, preview_data_root=None, install_catalog=None, catalog_sha256=None, translation_models_root=None, *, local_package=None):
        self.session_id, self.output = session_id, output
        self.output_lock = threading.Lock()
        self.transfer_lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.sequence = 0
        self.hello = False
        self.seen = set()
        self.seen_order = deque()
        self.operation = None
        self.last_finished = None
        self.thread = None
        self.tasks = None
        self.workbench = None
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.endpoint_override = endpoint
        self.configuration_lock = threading.RLock()
        self.local_package = local_package
        self.speech = None
        self.environment_thread = None
        self.environment_cancel = threading.Event()
        self.ollama_discovery = None
        self.dictionary_mode = 'bundled'
        self.dictionary_warning = None
        package_sources = local_package[0].parent/'Runtime/install-sources' if local_package else None
        self.managed = ManagedResources(preview_data_root, install_catalog, catalog_sha256,
                                        package_sources=package_sources) if preview_data_root else None
        self.service = (TranslationService(preview_data_root, self.managed, translation_models_root)
                        if preview_data_root and not local_package else None)
        defaults = None
        if local_package:
            from translator_core.local_environment import DEFAULT_ENDPOINT as OLLAMA_ENDPOINT
            from translator_core.preferences import DEFAULTS
            from translator_core.translation_routes import AUTO_MODEL
            defaults = dict(DEFAULTS, endpoint=OLLAMA_ENDPOINT, translation_model=AUTO_MODEL)
        self.preferences = (Preferences(preview_data_root, defaults=defaults, external_only=bool(local_package))
                            if preview_data_root else None)
        self.dictionary_config = dictionary_config
        if not local_package and self.managed and self.managed.baseline is not None:
            dictionary_config = self.managed_dictionary_config()
        self.pdf_warning = None
        self.ocr_warning = None
        self.image_warning = None
        if task_root:
            store = TaskStore(task_root); adapter = ocr = None
            try: adapter = platform_pdf_adapter(store.root / "page-handoff", pdf_helper, owned_python(), pdf_library)
            except (CoreError, OSError) as exc: self.pdf_warning = str(exc)
            try: ocr = platform_ocr_adapter(store.root / 'ocr-handoff', adapter, pdf_helper)
            except (CoreError, OSError) as exc: self.ocr_warning = str(exc)
            try: image = platform_image_adapter(store.root / 'image-handoff', image_helper)
            except (CoreError, OSError) as exc: image = None; self.image_warning = str(exc)
            capture_helper = Path(__file__).resolve().parents[1] / 'shared/platform/macos/AudioCapture.app/Contents/MacOS/AudioCapture'
            if not local_package and not capture_helper.is_file():
                capture_helper = Path(__file__).resolve().parents[3] / 'artifacts/t08-20261110-run1/bin/AudioCapture.app/Contents/MacOS/AudioCapture'
            self.tasks = CaptureTasks(store, platform_boundaries(boundary_helper), adapter, ocr_adapter=ocr, image_adapter=image,
                                   audio_adapter=MacAudio(store.root.parent / "audio-executions", Path(__file__).resolve().parents[1] / "shared/platform/macos/audio-playback") if sys.platform == "darwin" else None,
                                   capture_adapter=MacCapture(store.root.parent / 'capture-executions', capture_helper) if sys.platform == 'darwin' else None,
                                   engine_factory=self.translation_engine, preferences=self.preferences,
                                   resolve_task_values=self.external_translation_values if local_package else None)
            if local_package:
                from translator_core.workbench import Workbench
                self.workbench = Workbench(self.tasks)
        # Explicit resources, completely separate from the translation task root.
        from translator_core.dictionary.resources import ResourceCatalog
        if local_package:
            self.bundled_dictionary = package_sources/'dictionary/resources.json'
            self.dictionary_choice = Path(preview_data_root)/'resources/dictionary-choice.json'
            probe = self.initial_dictionary(dictionary_config, task_root)
        else:
            probe = ResourceCatalog(dictionary_config, forbidden_roots=(task_root,) if task_root else (), lazy=True)
        self.dictionary = DictionaryWorker(session_id, self.emit, catalog=probe)
        self.history = HistoryWorker(self)
        self.resources = ResourceManager(preview_data_root, probe, self.resource_adapters(), probe_resources) if preview_data_root else None
        if local_package:
            package, identity = local_package
            initialize_package_resources(package, identity, preview_data_root, managed=self.managed, manager=self.resources)
            if identity.get('external_speech_presets'):
                self.speech = SpeechLibrary(package, Path(preview_data_root), self.tasks.audio_adapter)
        if self.resources: self.resources.restore_dictionary()
        if self.tasks: self.tasks.resources = self.resources

    def initial_dictionary(self, explicit, task_root):
        from translator_core.dictionary.resources import ResourceCatalog
        from translator_core.local_environment import dictionary_catalog
        from translator_core.resources import json_value, safe_read
        from .dictionary_worker import QueryCancel
        choice = explicit
        try:
            if not choice and (self.dictionary_choice.exists() or self.dictionary_choice.is_symlink()):
                value = json_value(safe_read(self.dictionary_choice, 8192))
                if value.get('version') != 'dictionary-choice-v1':
                    raise ValueError("dictionary-choice-v1")
                choice = value['path']
                if choice is not None and (not isinstance(choice, str) or not Path(choice).is_absolute()):
                    raise ValueError("dictionary path")
            if choice:
                limit = QueryCancel()
                limit.deadline = time.monotonic() + 10
                catalog = dictionary_catalog(choice, task_root, limit)
                self.dictionary_mode = 'custom'
                return catalog
        except (CoreError, OSError, TypeError, ValueError, KeyError, AttributeError):
            self.dictionary_warning = '自选词典无法读取，已改用内置词典。'
        limit = QueryCancel()
        limit.deadline = time.monotonic() + 12
        try:
            return dictionary_catalog(self.bundled_dictionary, task_root, limit)
        except (CoreError, OSError, TypeError, ValueError):
            self.dictionary_warning = '内置词典无法读取，可选择其他词典配置。'
            return ResourceCatalog(lazy=True)

    def environment_request(self, req):
        try:
            if not self.local_package:
                raise CoreError('当前候选没有本地安装资源入口。', 'unsupported_operation')
            kind = req['type']
            if kind == 'environment.scan':
                if self.environment_thread and self.environment_thread.is_alive():
                    raise CoreError('正在检测本地资源。', 'busy')
                with self.configuration_lock:
                    revision = self.resources.revision
                    if not self.operation:
                        package, identity = self.local_package
                        initialize_package_resources(package, identity, self.resources.root.parent,
                                                     managed=self.managed, manager=self.resources)
                    specs = deepcopy((self.resources.saved_selection or {}).get('resources', []))
                    prefs = deepcopy(self.preferences.saved)
                    observed = self.resources.describe()
                    changed = observed if self.resources.revision != revision else None
                    verified = any(row['capability'] == 'speech.continuous_asr' and row['prerequisite_satisfied']
                                   for row in observed['capabilities'])
                    dictionary = {'mode': self.dictionary_mode, 'warning': self.dictionary_warning}
                self.environment_thread = threading.Thread(target=self.scan_environment,
                    args=(req, specs, prefs, verified, dictionary, changed), daemon=False)
                self.environment_thread.start()
                return
            if not self.resources or self.resources.dirty or self.resources.config_blocked:
                raise CoreError('请先在高级设置中处理待确认的资源配置。', 'selection_unsaved')
            if self.tasks.save_blocked:
                raise CoreError('请先处理待确认的任务保存。', 'save_blocked')
            from translator_core.local_environment import SPEECH_FOLDERS
            item, path = req.get('item'), req.get('path')
            if self.speech:
                if kind == 'environment.speech_choice':
                    with self.configuration_lock:
                        self.speech.select(req.get('mode'), item)
                    self.emit('environment.choice', req, choices=self.speech.state['choices'])
                    return
                if kind in ('environment.speech_import', 'environment.speech_verify'):
                    from translator_core.speech_presets import preset
                    if kind == 'environment.speech_import':
                        part = req.get('part')
                        if part not in ('model', 'assets', 'runtime') or not isinstance(path, str):
                            raise CoreError('请选择本地模型、辅助资源或运行包。', 'speech_import')
                        no_links(path)
                        if part != 'runtime':
                            preset(item)
                    else:
                        preset(item)
                    self.start_text(req)
                    return
                if kind != 'environment.dictionary':
                    raise CoreError('请从语音资源页导入本机文件。', 'unsupported_operation')
            if kind == 'environment.download' and (
                    req.get('user_requested') is not True or item not in SPEECH_FOLDERS):
                raise CoreError('请使用资源页的下载按钮选择固定版本。', 'download_source')
            file_actions = ('environment.speech_folder', 'environment.runtime_zip', 'environment.tokenizer')
            if kind in (*file_actions, 'environment.dictionary'):
                if ((kind in file_actions or path is not None) and
                        (not isinstance(path,str) or len(path.encode())>4096 or not Path(path).is_absolute())
                        or kind=='environment.speech_folder' and item not in {*SPEECH_FOLDERS,'speech.runtime'}):
                    raise CoreError('请选择有效的本机资源位置。', 'unsafe_path')
            elif kind not in ('environment.download','environment.restore_speech'):
                raise CoreError('不支持此资源操作。', 'unsupported_operation')
            self.start_text(req)
        except (CoreError, OSError, TypeError, ValueError) as exc:
            self.emit('environment.error', req, error=dict(message=str(exc), code=getattr(exc, 'code', 'environment_error')))

    def scan_environment(self, req, specs, prefs, verified, dictionary, changed):
        from translator_core.local_environment import (environment_status, external_ollama_route,
                                                       ollama_addresses, speech_files_present)
        try:
            configured_endpoint = self.endpoint_override or prefs['endpoint']
            addresses = ollama_addresses(configured_endpoint, explicit=self.endpoint_override is not None)
            report = environment_status(addresses, specs, verified, self.environment_cancel)
            package, identity = self.local_package
            root = self.resources.root.parent
            defaults = package_resource_defaults(package,identity,root,self.managed,dict(resources=specs))
            variants = package_speech_variants(package,root,defaults,dict(resources=specs))
            voice = report['whisper']
            voice['bundled'] = bool(identity.get('bundled_whisper'))
            voice['variants'] = [name for name,spec in variants.items() if speech_files_present(spec)]
            voice['model_sources'] = {name: speech_origin(variants[name]['root'],root) for name in voice['variants']}
            voice['sources'] = {spec['resource_id']: dict(origin=speech_origin(spec['root'],root),
                               path=spec['root'],version=spec['version']) for spec in specs
                               if spec['resource_id'] in ('speech.runtime','speech.model','speech.assets')}
            if self.speech:
                report['speech_presets'] = self.speech.report(defaults, dict(resources=specs))
            services = report['ollama']['external']
            with self.configuration_lock:
                self.ollama_discovery = (configured_endpoint, services)
            report['ollama']['selected'] = external_ollama_route(services, prefs['translation_model'])
            self.emit('environment.description', req, description=dict(report, dictionary=dictionary),
                      resources_revision=changed['selection_revision'] if changed else None)
        except (Cancelled, OSError, CoreError, TypeError, ValueError) as exc:
            self.emit('environment.error', req, error=dict(message=str(exc), code=getattr(exc, 'code', 'environment_error')))

    def apply_environment(self, req, op):
        from translator_core.local_environment import (dictionary_catalog, fetch_speech, import_dictionary,
                                                       import_speech_folder, import_speech_tokenizer,
                                                       import_whisper_runtime, speech_files_present, speech_target)
        from translator_core.resources import atomic_json
        kind, cancel = req['type'], op['cancel']
        package, identity = self.local_package
        data_root = self.resources.root.parent
        item = req.get('item')
        last_progress = 0.0

        def progress(done, total):
            nonlocal last_progress
            now = time.monotonic()
            if done == total or now-last_progress >= .25:
                self.emit('environment.progress', req, operation_id=op['id'], lease_id=op['lease'],
                          received=done, total=total)
                last_progress = now

        if kind == 'environment.dictionary':
            chosen = req.get('path')
            deadline = time.monotonic() + (600 if chosen else 45)

            class DictionaryCheck:
                def is_set(self):
                    return cancel.is_set() or time.monotonic() >= deadline

            check = DictionaryCheck()
            if chosen:
                catalog = import_dictionary(chosen, data_root, self.tasks.store.root,
                                            self.dictionary.catalog.config_path, check, progress)
                chosen = str(catalog.config_path)
            else:
                catalog = dictionary_catalog(self.bundled_dictionary, self.tasks.store.root, check)
            with self.configuration_lock:
                if check.is_set():
                    raise Cancelled()
                atomic_json(self.dictionary_choice, dict(version='dictionary-choice-v1', path=chosen))
                self.dictionary.close()
                self.dictionary = DictionaryWorker(self.session_id, self.emit, catalog=catalog)
                self.resources.dictionary = catalog
                self.dictionary_mode = 'custom' if chosen else 'bundled'
                self.dictionary_warning = None
                return {'dictionary': self.dictionary.description()}
        if self.speech and kind in ('environment.speech_import', 'environment.speech_verify'):
            selection, defaults = self.speech_defaults()
            if kind == 'environment.speech_import':
                key, record = self.speech.stage_import(item, req['part'], Path(req['path']),
                                                      defaults, selection, cancel, progress)
                with self.operation_lock, self.configuration_lock:
                    if cancel.is_set():
                        raise Cancelled()
                    self.tasks.audio_adapter.close()
                    self.speech.commit(roots=dict(self.speech.state['roots'], **{key: record}))
                    op['committed'] = True
                return {'message': '资源已核对并保存。资源齐备后可运行本机验证。'}
            specs = self.speech.specs(item, defaults, selection)
            result = self.speech.verify(item, specs, cancel, lambda message: self.emit(
                'environment.progress', req, operation_id=op['id'], lease_id=op['lease'], message=message))
            with self.operation_lock, self.configuration_lock:
                if cancel.is_set():
                    raise Cancelled()
                atomic_json(self.speech.check_root/f'{item}.json', result)
                op['committed'] = True
            failures = [outcome['message'] for outcome in result['modes'].values() if not outcome['passed']]
            return {'message': '能力验证完成。' if not failures else '部分能力未通过，请查看该模型的状态与结果。'}
        if kind=='environment.restore_speech':
            with self.operation_lock, self.configuration_lock:
                if cancel.is_set():
                    raise Cancelled()
                self.tasks.audio_adapter.close()
                restore_bundled_speech(package,identity,self.resources)
                op['committed'] = True
                return {'resources':self.resources.describe()}
        selection = self.resources.saved_selection
        defaults = package_resource_defaults(package, identity, data_root, self.managed, selection)
        if kind == 'environment.runtime_zip':
            item = 'speech.runtime'
        elif kind=='environment.tokenizer':
            item = 'speech.assets'
        spec = next(row for row in defaults['resources'] if row['resource_id'] == item)
        if kind == 'environment.speech_folder' and item=='speech.model':
            variants = package_speech_variants(package,data_root,defaults,selection)
            spec = next((row for row in variants.values()
                         if speech_files_present(dict(row,root=req['path']))), None)
            if not spec:
                raise CoreError('请选择完整的 Whisper small.en 或 large-v3-turbo 模型文件夹。','audio_binding')
        selected_spec = next((row for row in (selection or {}).get('resources', []) if row['resource_id']==item), None)
        selected_root = Path(selected_spec['root']) if selected_spec else None
        if kind == 'environment.runtime_zip':
            root = import_whisper_runtime(Path(req['path']), package/'runtime/component-catalog.json', spec, data_root, cancel)
        elif kind=='environment.tokenizer':
            root = import_speech_tokenizer(Path(req['path']), selected_spec, spec, data_root, cancel, progress)
        elif kind == 'environment.download':
            root = speech_target(data_root, item, spec)
            fetch_speech(spec, root, cancel, progress)
        else:
            root = import_speech_folder(Path(req['path']), spec, data_root, selected_root, cancel, progress)
        with self.operation_lock, self.configuration_lock:
            if cancel.is_set():
                raise Cancelled()
            if self.tasks.audio_adapter and (root != selected_root or kind != 'environment.speech_folder'):
                self.tasks.audio_adapter.close()
            select_speech_root(package, identity, data_root, self.managed, self.resources, item, root,
                               model_spec=spec if item=='speech.model' else None)
            op['committed'] = True
            return {'resources': self.resources.describe()}

    def managed_dictionary_config(self):
        if self.managed.active.get('dictionary') and not self.managed.blocked:
            try:
                return str(self.managed.active_path('dictionary')/'resources.json')
            except (OSError, ResourceError):
                pass
        return str(self.managed.root/'dictionary-unavailable.json')

    def external_translation_values(self, values, fixed_model=None):
        from translator_core.local_environment import discover_ollama, external_ollama_route, ollama_addresses
        configured_endpoint = self.endpoint_override or values['endpoint']
        addresses = ollama_addresses(configured_endpoint, explicit=self.endpoint_override is not None)
        discovery = self.operation['ollama_discovery'] if self.operation else self.ollama_discovery
        if not discovery or discovery[0] != configured_endpoint:
            cancel = self.operation['cancel'] if self.operation else self.environment_cancel
            discovery = (configured_endpoint, discover_ollama(addresses, cancel))
            if self.operation:
                self.operation['ollama_discovery'] = discovery
            else:
                self.ollama_discovery = discovery
        model = fixed_model or values['translation_model']
        selected = external_ollama_route(discovery[1], model, exact=fixed_model is not None)
        endpoint = selected['endpoint'] if selected else addresses[0]
        return dict(values, translation_backend='external_ollama', endpoint=endpoint,
                    translation_model=selected['model'] if selected else model)

    def translation_engine(self, cancel):
        task = self.tasks.task if self.tasks else None
        execution = task.get('execution') if task else None
        endpoint = self.endpoint
        if self.local_package:
            if execution is not None:
                validate_execution(execution)
            if execution and execution['translation_backend'] == 'external_ollama':
                endpoint = self.endpoint_override or execution['endpoint']
            else:
                model = task['configuration'].get('model') if task else None
                endpoint = self.external_translation_values(self.preferences.saved, fixed_model=model)['endpoint']
            return Ollama(endpoint, cancel=cancel, require_mac_version=True)
        if execution is not None:
            validate_execution(execution)
            if execution['translation_backend'] == 'application_ollama':
                if self.endpoint_override is not None:
                    raise CoreError('本应用后端需要专用服务，请清除命令行外部端点覆盖。', 'backend_override')
                endpoint = self.service.ensure(cancel, task['configuration'].get('model'))
            else:
                endpoint = self.endpoint_override or execution['endpoint']
        return Ollama(endpoint, cancel=cancel)

    def resolve_workbench(self, model, direction, op):
        from translator_core.local_environment import ollama_addresses
        from translator_core.preferences import execution_for
        from translator_core.translation_routes import resolve_recipe
        values = self.preferences.require_confirmed()
        failure = None
        for address in ollama_addresses(self.endpoint_override or values['endpoint'], explicit=bool(self.endpoint_override)):
            engine = Ollama(address, cancel=op['run_cancel'], require_mac_version=True)
            op['engine'] = engine
            if op['cancel'].is_set():
                raise Cancelled()
            try:
                recipe, version = resolve_recipe(engine, model or values['translation_model'], direction,
                                                 self.tasks.boundaries.splitter_version)
                execution = execution_for(dict(values, endpoint=address), self.preferences.revision)
                return recipe, version, engine, execution
            except CoreError as exc:
                failure = exc
                if exc.code not in ('service_unavailable', 'model_mismatch'):
                    raise
        if failure.code == 'service_unavailable':
            raise CoreError('本机 Ollama 暂不可用。请启动服务并重新检测，原文可以继续编辑。',failure.code) from failure
        raise failure

    def audio_setup(self, req, op, *, live=False):
        from translator_core.languages import speech_language
        from translator_core.translation_routes import verify_direction
        direction, variant = req.get('direction'), req.get('asr_model')
        language = speech_language(variant,direction)
        binding = None
        if live and self.speech:
            binding = self.audio_resource_binding(req,op,variant,language,'speech.continuous_asr')
        recipe, version, engine, execution = self.resolve_workbench(req.get('model'),direction,op)
        verify_direction(engine,recipe,version,self.workbench.evidence)
        setup = dict(recipe=recipe,execution=execution,asr_model=variant)
        if live:
            setup['binding'] = binding or self.audio_resource_binding(req,op,variant,language,'speech.continuous_asr')
        return setup

    def speech_defaults(self):
        package, identity = self.local_package
        selection = self.resources.saved_selection
        return selection, package_resource_defaults(package, identity, self.resources.root.parent,
                                                    self.managed, selection)

    def audio_resource_binding(self, req, op, variant, language, capability):
        if self.speech:
            from translator_core.speech_presets import MODES, preset
            row = preset(variant)
            if capability not in row['modes']:
                raise CoreError(f"{row['name']} 本版未开放{MODES[capability]}。", 'audio_binding')
            selection, defaults = self.speech_defaults()
            specs = self.speech.specs(variant, defaults, selection)
            record = self.speech.current_check(variant, specs)
            if not record or not record['modes'].get(capability, {}).get('passed'):
                raise CoreError(f"请在「偏好设置 → 语音资源」为 {row['name']} 补齐资源并完成{MODES[capability]}验证。",
                                'capability_unverified')
            return self.speech.binding(variant, language, capability, specs, record)
        from translator_core.local_environment import speech_files_present, whisper_runtime_present
        from translator_core.resources import EXECUTION
        package, identity = self.local_package
        root = self.resources.root.parent
        selection = self.resources.saved_selection
        specs = {row['resource_id']: row for row in (selection or {}).get('resources', [])}
        defaults = package_resource_defaults(package,identity,root,self.managed,selection)
        model = package_speech_variants(package,root,defaults,selection)[variant]
        if not speech_files_present(model):
            raise CoreError(f'找不到 Whisper {variant}。请在「偏好设置 → 语音资源」导入该模型。','audio_binding')
        if not whisper_runtime_present(specs.get('speech.runtime')):
            raise CoreError('Whisper 运行组件不可用。请在「偏好设置 → 语音资源」恢复内置资源或导入兼容运行包。','audio_binding')
        if not speech_files_present(specs.get('speech.assets')):
            raise CoreError('分词与 VAD 文件不可用。请在「偏好设置 → 语音资源」恢复内置资源或导入完整资源。','audio_binding')
        if op['cancel'].is_set():
            raise Cancelled()
        model_before = specs.get('speech.model',{})
        if (model_before.get('root'),model_before.get('manifest')) != (model['root'],model['manifest']):
            with self.configuration_lock:
                self.tasks.audio_adapter.close()
                select_speech_root(package,identity,root,self.managed,self.resources,'speech.model',
                                   Path(model['root']),model_spec=model)
        execution = dict(EXECUTION,language=language)
        try:
            return json.loads(self.resources.make_binding(capability,execution))
        except ResourceError as exc:
            if exc.code!='capability_unverified':
                raise
        owner = self.resources.owner(self.session_id,req['request_id'],op['id'])
        self.snapshot(req,dict(audio_status=f'正在检查 Whisper {variant} 与运行环境…'),op)
        description = self.resources.check(owner,op['cancel'],lambda *_: None,runtime=True)
        self.snapshot(req,dict(audio_resources=description),op)
        try:
            return json.loads(self.resources.make_binding(capability,execution))
        except ResourceError as exc:
            raise CoreError('语音资源检查未通过。请在「偏好设置 → 语音资源」查看检查结果并重试。',exc.code) from exc

    def workbench_request(self, req):
        if not self.workbench:
            self.error(req,'unsupported_operation'); return
        try:
            kind=req['type']
            if kind in WORKBENCH_OPERATIONS:
                self.start_text(req)
            elif kind=='workbench.read':
                record=self.workbench.read(req.get('task_id'),req.get('locator'))
                self.snapshot(req,dict(workbench_record=record))
                self.emit('task.done',req)
            elif kind=='workbench.stop':
                with self.operation_lock:
                    op=self.operation
                    owner=(req.get('operation_id'),req.get('lease_id'))
                    if op and owner==(op['id'],op['lease']) and op.get('can_stop') and not op.get('committed'):
                        op['stop']=True
                        op['run_cancel'].set()
                        if op.get('engine'):
                            op['engine'].cancel()
                    elif not op or owner!=(op['id'],op['lease']):
                        if owner != self.last_finished:
                            raise CoreError('文档操作已改变。','operation_or_lease_mismatch')
                self.emit('task.done',req)
            else:
                self.error(req,'unsupported_operation')
        except (CoreError,OSError,ValueError,TypeError) as exc:
            self.error(req,getattr(exc,'code','workbench_error'),str(exc))

    def run_workbench(self, req, op):
        def send(details):
            self.snapshot(req,dict(workbench_update=details),op)

        def commit(publish):
            with self.operation_lock:
                if op['cancel'].is_set():
                    raise Cancelled()
                publish()
                op['committed']=True

        mode=req.get('mode')
        if req['type']=='workbench.select':
            selection=self.workbench.choose(mode,req.get('source_path'),op['run_cancel'],commit,clipboard=req.get('clipboard') is True)
            send(dict(selection=selection))
            return 'completed',dict(message='原文可以直接编辑，或先提取原文。')
        plan=self.workbench.read_plan(req)
        if plan['mode']!=mode:
            raise CoreError('提交任务类型不一致，请重新提交。','invalid_input')
        try:
            status, detail=self.workbench.execute(plan,req['type']=='workbench.extract',op,
                                                 self.resolve_workbench,send,commit)
        except Cancelled:
            if op.get('stop') and not op['cancel'].is_set():
                return 'stopped',dict(message='已停止。本次没有已完成页。')
            raise
        if 'results' in detail:
            results=detail.pop('results')
            send(dict(results=results,direction=detail['direction'],model=detail['model']))
        return status,detail

    def preferences_request(self, req):
        try:
            if not self.preferences:
                raise CoreError('需要明确的应用数据根。', 'preferences_missing')
            kind = req['type']
            if kind == 'preferences.describe':
                self.emit('preferences.description', req, description=self.preferences.describe())
                return
            if self.operation or self.tasks and self.tasks.save_blocked:
                raise CoreError('请等待当前操作与任务保存完成，再更改偏好。', 'busy')
            if (req.get('revision'), req.get('generation')) != (self.preferences.revision, self.preferences.generation):
                raise CoreError('偏好修订已变化，请刷新后重试。', 'preferences_changed')
            if kind == 'preferences.stage':
                result = self.preferences.stage(req.get('values'))
            elif kind == 'preferences.save':
                if 'values' in req:
                    self.preferences.stage(req['values'])
                result = self.preferences.save()
            elif kind == 'preferences.discard':
                result = self.preferences.discard()
            elif kind == 'preferences.reload':
                result = self.preferences.reload()
            elif kind == 'preferences.confirm':
                result = self.preferences.confirm()
            else:
                raise CoreError('偏好动作不受支持。', 'preferences_invalid')
            self.emit('preferences.description', req, description=result)
        except (CoreError, OSError, TypeError, ValueError) as exc:
            self.emit('preferences.error', req, error=dict(code=getattr(exc,'code','preferences_io'), message=str(exc), retryable=True),
                      description=self.preferences.describe() if self.preferences else None)

    def managed_request(self, req):
        try:
            if not self.managed:
                raise ResourceError('resource_missing', '需要明确的应用数据根。')
            if req['type'] == 'managed.describe':
                self.emit('managed.description', req, description=self.managed.describe())
                return
            if req['type'] != 'managed.operate':
                raise ResourceError('resource_incompatible', '资源动作不受支持。')
            if self.operation or self.tasks and self.tasks.save_blocked:
                raise ResourceError('busy', '请等待当前操作与任务保存完成。')
            self.preferences.require_confirmed()
            if self.resources.dirty:
                raise ResourceError('selection_unsaved', '请先保存或放弃资源引用。')
            if (req.get('revision'), req.get('generation')) != (self.managed.revision, self.managed.generation):
                raise ResourceError('activation_changed', '资源激活修订已改变，请刷新。')
            if req.get('action') not in ('prepare','repair','rollback','uninstall','recover') or type(req.get('confirmed',False)) is not bool:
                raise ResourceError('resource_incompatible', '资源动作或确认字段无效。')
            self.start_text(req)
        except (CoreError, OSError, TypeError, ValueError) as exc:
            self.emit('managed.error', req, error=translate_error(exc), description=self.managed.describe() if self.managed else None)

    def protected_resources(self):
        task = self.tasks.task if self.tasks else None
        result = []
        for binding in (task or {}).get('audio',{}).get('bindings',{}).values():
            result.extend(value['root'] for value in binding.get('resources',{}).values())
        return result

    def drain_managed(self, component):
        from translator_core.dictionary.resources import ResourceCatalog
        with self.configuration_lock:
            if component == 'speech-runtime' and self.tasks.audio_adapter:
                self.tasks.audio_adapter.close()
            if component == 'translation-engine' and self.service:
                self.service.close()
            if component == 'dictionary' and not self.local_package:
                self.dictionary.close()
                config = self.managed.root/'dictionary-changing.json'
                catalog = ResourceCatalog(config, lazy=True)
                self.dictionary = DictionaryWorker(self.session_id, self.emit, str(config), catalog=catalog)
                self.resources.dictionary = catalog
            self.resources.last = None
            self.resources.service_evidence = None

    def synchronize_managed(self, cancel):
        from translator_core.resources import read_reference, REFERENCE
        from translator_core.dictionary.resources import ResourceCatalog
        with self.configuration_lock:
            selected = self.resources.selection
            if self.local_package:
                package, identity = self.local_package
                initialize_package_resources(package, identity, self.resources.root.parent,
                                             managed=self.managed, manager=self.resources)
            elif selected:
                reference = read_reference(selected['path'])
                payload = (self.managed.active_path('speech-runtime') if self.managed.active.get('speech-runtime')
                           else self.managed.root/'speech-unavailable')
                for spec in reference['resources']:
                    if spec['resource_id'] in ('speech.runtime', 'speech.backend'):
                        spec['root'] = str(payload)
                        if spec['resource_id'] == 'speech.runtime':
                            spec['manifest']['path'] = str(payload/'component.json')
                value = dict(version=REFERENCE, title='T09-B当前激活运行资源', resources=reference['resources'])
                path = self.resources.root/('managed-reference-'+str(self.managed.revision)+'.json')
                from translator_core.resources import atomic_json
                atomic_json(path, value)
                if reference['resources'] != selected['resources']:
                    self.resources.select(path)
                    self.resources.save()
            if not self.local_package and self.managed.baseline is not None:
                config = self.managed_dictionary_config()
                self.dictionary.close()
                catalog = ResourceCatalog(config, forbidden_roots=(self.tasks.store.root,), lazy=True)
                self.dictionary = DictionaryWorker(self.session_id, self.emit, str(config), catalog=catalog)
                self.resources.dictionary = catalog
                self.resources.last = None

    def resource_adapters(self):
        values = {}
        if self.tasks:
            for name, adapter in (("pdf", self.tasks.adapter), ("ocr", self.tasks.ocr_adapter), ("image", self.tasks.image_adapter)):
                values[name] = dict(identity=adapter.identity, state="runtime_verified",
                    languages=getattr(adapter, 'languages', None), profile=adapter.probe.get('profile'),
                    meaning="沿用既有事实适配器能力；未处理文档或图片", asset_hash="not_exposed_by_system") if adapter else dict(state='missing')
        return values

    def emit(self, kind, req, **fields):
        with self.output_lock:
            self.sequence += 1
            self.output.write(encode(dict(protocol=PROTOCOL, session_id=self.session_id,
                request_id=req["request_id"], sequence=self.sequence, type=kind, **fields)))
            self.output.flush()

    def error(self, req, code, message=None):
        self.emit("resource.error" if req.get('type', '').startswith('resource.') else "dictionary.error" if req.get('type', '').startswith('dictionary.') else "error", req,
                  error={"code": code, "message": message or code, "retryable": False})

    def handle(self, req):
        if req["session_id"] != self.session_id:
            self.error(req, "session_mismatch"); return True
        if req["request_id"] in self.seen:
            self.error(req, "duplicate_request"); return True
        self.seen.add(req["request_id"]); self.seen_order.append(req["request_id"])
        if len(self.seen_order) > 1024:
            self.seen.remove(self.seen_order.popleft())
        kind = req["type"]
        if kind == "hello" and not self.hello:
            # Confirmation is emitted only after the real request loop is entered.
            self.hello = True
            info = self.diagnostics()
            if self.tasks: info["splitter_version"] = self.tasks.boundaries.splitter_version
            info["pdf_adapter"] = self.tasks.adapter.identity if self.tasks and self.tasks.adapter else self.pdf_warning
            self.emit("ready", req, diagnostics=info)
        elif not self.hello:
            self.error(req, "handshake_required")
        elif kind == "diagnose":
            with self.configuration_lock:
                info = self.diagnostics()
            if self.tasks: info["splitter_version"] = self.tasks.boundaries.splitter_version
            info["pdf_adapter"] = self.tasks.adapter.identity if self.tasks and self.tasks.adapter else self.pdf_warning
            self.emit("diagnostics", req, diagnostics=info)
        elif kind.startswith("history."):
            self.history.handle(req)
        elif kind.startswith("dictionary."):
            with self.configuration_lock:
                self.dictionary.handle(req)
        elif kind.startswith('preferences.'):
            with self.configuration_lock:
                self.preferences_request(req)
        elif kind.startswith('managed.'):
            self.managed_request(req)
        elif kind.startswith('environment.'):
            self.environment_request(req)
        elif kind.startswith("resource."):
            with self.configuration_lock:
                self.resource_request(req)
        elif kind.startswith('workbench.'):
            self.workbench_request(req)
        elif kind.startswith(("task.", "document.", "image.", "audio.", "capture.")) or kind == "service.check":
            with self.configuration_lock:
                self.text_request(req)
        elif kind == "lease.renew":
            with self.operation_lock:
                op = self.operation
                if not op or (req.get("operation_id"), req.get("lease_id")) != (op["id"], op["lease"]):
                    self.error(req, "operation_or_lease_mismatch")
                elif op["cancel"].is_set():
                    self.error(req, "operation_cancelling")
                else:
                    op["deadline"] = time.monotonic() + op["lease_ms"] / 1000
                    self.emit("lease.renewed", req, operation_id=op["id"], lease_id=op["lease"])
        elif kind == "probe.start":
            self.start(req)
        elif kind == "cancel":
            with self.operation_lock:
                op = self.operation
                identity = (req.get("operation_id"), req.get("lease_id"))
                if not op and identity == self.last_finished:
                    self.emit("cancel.already_finished", req, operation_id=identity[0], lease_id=identity[1])
                elif not op or identity != (op["id"], op["lease"]):
                    self.error(req, "operation_or_lease_mismatch")
                elif op.get('committed'):
                    self.emit('cancel.already_finished',req,operation_id=identity[0],lease_id=identity[1])
                else:
                    self.emit("cancel.accepted", req, operation_id=op["id"], lease_id=op["lease"])
                    op["cancel"].set()
                    if op.get('run_cancel'): op['run_cancel'].set()
                    if op.get("engine"): op["engine"].cancel()
                    if self.tasks: self.tasks.cancel(op["id"])
        elif kind == "shutdown":
            self.drain()
            self.emit("shutdown.confirmed", req, tail_drained=True)
            return False
        else:
            self.error(req, "unsupported_operation")
        return True

    def resource_request(self, req):
        try:
            if not self.resources: raise ResourceError('resource_missing', '需要显式本包数据根。')
            kind = req['type']
            if kind == 'resource.describe':
                self.emit('resource.description', req, description=self.resources.describe()); return
            if self.operation: raise ResourceError('busy', '请等待当前操作结束，再更改或检查资源。')
            if self.tasks and self.tasks.save_blocked: raise ResourceError('busy', '请先完成当前任务的保存。')
            selected=self.resources.selection
            if (req.get('selection_revision'),req.get('resource_set_id'))!=(self.resources.revision,selected['resource_set_id'] if selected else None):
                raise ResourceError('selection_changed','操作不属于当前资源选择，请刷新核对。')
            if kind == 'resource.check':
                if type(req.get('runtime')) is not bool: raise ResourceError('resource_incompatible','检查范围无效。')
                self.start_text(req); return
            if kind == 'resource.select': description = self.resources.select(req.get('reference_path'))
            elif kind == 'resource.save': description = self.resources.save()
            elif kind == 'resource.discard': description = self.resources.discard()
            elif kind == 'resource.bind':
                value = self.resources.make_binding(req.get('capability'), req.get('execution'))
                self.emit('resource.binding', req, binding=json.loads(value)); return
            else: raise ResourceError('unsupported_resource_operation','当前资源操作不受支持。')
            self.emit('resource.description', req, description=description)
        except (CoreError, OSError, TypeError, ValueError) as exc:
            self.emit('resource.error', req, error=translate_error(exc),
                      description=self.resources.describe() if self.resources else None)

    def diagnostics(self):
        ocr = self.tasks.ocr_adapter if self.tasks else None
        state = 'requires_verified_ocr_adapter'
        if ocr:
            state = 'ready'
            try: ocr.require('en-zh'); ocr.require('zh-en')
            except CoreError: state = 'partial_offline_languages'
        info = diagnostics(self.tasks is not None, bool(self.tasks and self.tasks.adapter), state)
        info['ocr_adapter'] = dict(identity=ocr.identity, available_languages=ocr.languages, profile=ocr.probe['profile']) if ocr else self.ocr_warning
        info['text_rules'] = dict(identity=asdict(policy_identity()), execution_kinds=['translation'],
                                 explanation_generation=False)
        creation = self.tasks.last_text_creation if self.tasks else None
        info['last_text_creation'] = (dict(task_id=creation['task_id'],
            assessment=asdict(creation['assessment'])) if creation else None)
        image = self.tasks.image_adapter if self.tasks else None
        info['image_adapter'] = dict(identity=image.identity,available_languages=image.languages,profile=image.probe['profile']) if image else self.image_warning
        info['capabilities']['image'] = 'image-source-v1' if image else 'requires_image_adapter'
        info['capabilities']['image_ocr'] = 'image-ocr-v1' if image else 'requires_image_adapter'
        info['dictionary'] = self.dictionary.description()
        info['history'] = self.history.description()
        info['resources'] = self.resources.describe() if self.resources else None
        info['preferences'] = self.preferences.describe() if self.preferences else None
        info['managed'] = self.managed.describe() if self.managed else None
        return info

    def start(self, req):
        try:
            op_id, lease_id = identifier(req.get("operation_id")), identifier(req.get("lease_id"))
            steps, interval, lease_ms = req.get("steps", 20), req.get("interval_ms", 100), req.get("lease_ms", 10000)
            if (type(steps) is not int or not 1 <= steps <= MAX_STEPS
                    or type(interval) is not int or not 20 <= interval <= 250
                    or type(lease_ms) is not int or not 100 <= lease_ms <= 30000):
                raise ProtocolError("invalid_probe_bounds")
        except ProtocolError as exc:
            self.error(req, str(exc)); return
        with self.operation_lock:
            if self.operation is not None:
                self.error(req, "busy"); return
            op = {"id": op_id, "lease": lease_id, "cancel": threading.Event(), "lease_ms": lease_ms,
                  "deadline": time.monotonic() + lease_ms / 1000}
            self.operation = op
            self.emit("operation.started", req, operation_id=op_id, lease_id=lease_id,
                      steps=steps, lease_ms=lease_ms, result_kind="synthetic-lifecycle-probe")
            self.thread = threading.Thread(target=self.run_probe, args=(req, op, steps, interval), daemon=False)
            self.thread.start()

    def run_probe(self, req, op, steps, interval):
        accepted = []
        status = "completed"
        for step in range(1, steps + 1):
            if op["cancel"].wait(interval / 1000):
                status = "cancelled"; break
            if time.monotonic() >= op["deadline"]:
                status = "lease_expired"; break
            accepted.append(step)
            self.emit("progress", req, operation_id=op["id"], lease_id=op["lease"],
                      confirmed=step, total=steps)
        # Probe has no audio/model/DB tail. Preserve all confirmed steps and mark
        # the terminal result only after the finite operation has drained.
        with self.operation_lock:
            self.emit("operation.finished", req, operation_id=op["id"], lease_id=op["lease"],
                      status=status, tail_drained=True,
                      result={"kind": "synthetic-lifecycle-probe", "confirmed_steps": accepted,
                              "sha256": hashlib.sha256(encode({"steps": accepted})).hexdigest(),
                              "persistent_task": False, "inference": False})
            self.last_finished = (op["id"], op["lease"])
            self.operation = None

    def drain(self):
        self.environment_cancel.set()
        with self.operation_lock:
            if self.operation:
                self.operation["cancel"].set()
                if self.operation.get('run_cancel'): self.operation['run_cancel'].set()
                if self.operation.get("engine"): self.operation["engine"].cancel()
                if self.tasks: self.tasks.cancel(self.operation["id"])
        if self.thread:
            self.thread.join()
        if self.environment_thread:
            self.environment_thread.join()
        self.history.close()
        self.dictionary.close()
        if self.tasks and self.tasks.audio_adapter and hasattr(self.tasks.audio_adapter, 'close'):
            self.tasks.audio_adapter.close()
        if self.service:
            self.service.close()
        if self.workbench:
            self.workbench.close()

    def snapshot(self, req, snapshot, op=None):
        with self.transfer_lock:
            self._snapshot(req, snapshot, op)

    def _snapshot(self, req, snapshot, op=None):
        if self.tasks:
            snapshot = dict(snapshot, history_epoch=self.tasks.store.history_epoch)
        raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 32 * 1024 * 1024: raise CoreError("任务状态超出传输上限。")
        transfer = str(uuid.uuid4())
        fields = {} if op is None else dict(operation_id=op["id"], lease_id=op["lease"])
        self.emit("snapshot.begin", req, transfer_id=transfer, byte_count=len(raw), sha256=sha(raw), **fields)
        for index, offset in enumerate(range(0, len(raw), SNAPSHOT_CHUNK_BYTES)):
            self.emit("snapshot.chunk", req, transfer_id=transfer, index=index,
                      data=base64.b64encode(raw[offset:offset + SNAPSHOT_CHUNK_BYTES]).decode(), **fields)
        self.emit("snapshot.end", req, transfer_id=transfer, **fields)

    def text_request(self, req):
        if not self.tasks:
            self.error(req, "preview_root_required", "先指定独立预览任务目录。"); return
        try:
            kind = req["type"]
            if kind == 'capture.control':
                if not self.operation or (req.get('operation_id'), req.get('lease_id')) != (self.operation['id'], self.operation['lease']):
                    raise CoreError('采集控制操作所有者已改变。', 'capture_owner')
                self.tasks.capture_control(self.operation['id'], self.operation['lease'], req.get('task_id'),
                    req.get('capture_session'), req.get('epoch'), req.get('action'))
                self.emit('task.done', req)
                return
            if kind.startswith(('document.','image.')) and kind.split('.')[1] not in ('select','create','selection','clear_selection','render_selection'):
                expected='图片' if kind.startswith('image.') else '文档'
                if not self.tasks.task or self.tasks.task['kind']!=expected:
                    raise CoreError('操作不属于当前任务类型。','task_kind_mismatch')
            if kind.startswith("audio.") and kind not in ("audio.select", "audio.create", "audio.selection"):
                if not self.tasks.task or self.tasks.task["kind"] != "音频": raise CoreError("当前不是音频任务。", "task_kind_mismatch")
                if (req.get("task_id"), req.get("revision")) != (self.tasks.task["id"], self.tasks.task["revision"]):
                    raise CoreError("请求不属于当前音频修订。", "request_revision_mismatch")
            if kind == "audio.control":
                if not self.operation or (req.get("operation_id"), req.get("lease_id")) != (self.operation["id"], self.operation["lease"]):
                    raise CoreError("播放会话已改变。", "audio_playback")
                self.tasks.audio_control(self.operation["id"], req.get("action"), req.get("seconds"))
                self.emit("task.done", req); return
            if kind in ("capture.start", "capture.recover", "audio.select", "audio.asr", "audio.refine", "audio.play", "task.start", "service.check", "document.select", "document.extract", "document.ocr", "document.render", "image.select", "image.ocr", "image.render",'image.render_selection') or kind=='audio.create' and self.workbench:
                self.start_text(req); return
            if self.operation and kind not in ("task.list", "task.export"):
                raise CoreError("请先安全取消当前操作。", "busy")
            if kind in ("task.save", "task.export", 'document.draft', 'document.apply', 'document.discard', 'document.review', 'document.fork', 'image.draft','image.apply','image.discard','image.review','image.fork','image.retry_save'):
                task = self.tasks.task
                if not task or req.get("task_id") != task["id"] or req.get("revision") != task["revision"]:
                    raise CoreError("请求不属于当前任务修订。", "request_revision_mismatch")
            if kind == 'capture.describe':
                self.snapshot(req, dict(capture_description=self.tasks.describe_capture()))
                self.emit('task.done', req)
            elif kind == 'audio.export_wav':
                path = self.tasks.pcm_path()
                pcm = self.tasks.task['audio']['pcm']
                name = uuid.uuid4().hex + '.wav'
                from translator_core.store import atomic_copy
                atomic_copy(path, self.tasks.store.root / 'exports' / name, pcm['byteCount'], pcm['fingerprint'])
                self.emit('task.exported', req, filename=name, byte_count=pcm['byteCount'], sha256=pcm['fingerprint'])
            elif kind == "audio.selection":
                self.snapshot(req, dict(audio_selection=self.tasks.selected_audio())); self.emit("task.done", req)
            elif kind == "audio.retry_save":
                self.snapshot(req,self.tasks.retry_audio_save()); self.emit("task.done",req)
            elif kind == "audio.create":
                self.snapshot(req, self.tasks.create_audio(req.get("selection_token"))); self.emit("task.done", req)
            elif kind == "audio.draft":
                body=self.tasks.store.incoming(req.get("input_file"),req.get("sha256"),req.get("byte_count"),allow_empty=True)
                self.snapshot(req,self.tasks.audio_draft(req.get("cue_id"),req.get("cue_revision"),body)); self.emit("task.done",req)
            elif kind in ("audio.apply", "audio.discard"):
                self.snapshot(req,self.tasks.audio_apply(req.get("cue_id"),req.get("cue_revision"),kind=="audio.discard")); self.emit("task.done",req)
            elif kind == "audio.review":
                self.snapshot(req,self.tasks.audio_review(req.get("cue_id"),req.get("cue_revision"),req.get("reviewed"))); self.emit("task.done",req)
            elif kind == "task.list":
                items, errors = self.tasks.store.list()
                # A task list can also exceed a JSONL frame; use the same bounded transfer.
                self.snapshot(req, {"listing": items, "errors": errors})
                self.emit("task.done", req)
            elif kind in ('image.selection','image.clear_selection'):
                self.snapshot(req,self.tasks.selected_image() if kind=='image.selection' else self.tasks.clear_image_selection())
                self.emit('task.done',req)
            elif kind == "task.create":
                source = self.tasks.store.incoming(req.get("input_file"), req.get("sha256"), req.get("byte_count"))
                self.snapshot(req, self.tasks.create(source, req.get("direction")))
                self.emit("task.done", req)
            elif kind == 'image.create':
                self.snapshot(req,self.tasks.create_image(req.get('selection_token'),req.get('direction')))
                self.emit('task.done',req)
            elif kind == 'image.retry_save':
                self.snapshot(req,self.tasks.retry_image_save());self.emit('task.done',req)
            elif kind == "document.create":
                self.snapshot(req, self.tasks.create_document(req.get("selection_token"), req.get("first"), req.get("last"), req.get("direction"), req.get('mode','text')))
                self.emit("task.done", req)
            elif kind in ('document.draft','image.draft'):
                body=self.tasks.store.incoming(req.get('input_file'),req.get('sha256'),req.get('byte_count'),allow_empty=True)
                self.snapshot(req,self.tasks.draft(req.get('page'),req.get('page_revision'),body));self.emit('task.done',req)
            elif kind in ('document.apply','document.discard','image.apply','image.discard'):
                self.snapshot(req,self.tasks.apply_draft(req.get('page'),req.get('page_revision'),discard=kind in ('document.discard','image.discard')));self.emit('task.done',req)
            elif kind in ('document.review','image.review'):
                self.snapshot(req,self.tasks.review(req.get('page'),req.get('page_revision'),req.get('reviewed')));self.emit('task.done',req)
            elif kind in ('document.fork','image.fork'):
                self.snapshot(req,self.tasks.fork_document(req.get('fresh',False),req.get('direction')));self.emit('task.done',req)
            elif kind == "task.open":
                # Check trash before switching the worker's current task.
                self.tasks.store.history.guard_root()
                selected,_=self.tasks.store.read(req.get('task_id'))
                metadata_warning=None
                try:
                    meta=self.tasks.store.history.metadata(selected['id'],selected['kind'])
                except (CoreError,OSError) as exc:
                    metadata_warning='历史信息异常，仅可阅读/导出，原始数据保留：'+str(exc)
                else:
                    if meta['trashed_at'] is not None:raise CoreError('请先在回收站恢复此记录，再打开。','task_trashed')
                location = None
                if req.get('locator') is not None:
                    if metadata_warning:
                        location = dict(status='stale', message='历史信息异常，已打开可读内容，请核对后刷新定位。')
                    else:
                        location = self.tasks.store.history.resolve_locator(selected, req['locator'])
                snapshot=self.tasks.open(selected['id'])
                if metadata_warning:
                    self.tasks.warning=metadata_warning;self.tasks.save_blocked=True;snapshot=self.tasks.snapshot()
                if location is not None:
                    snapshot['history_location'] = location
                self.snapshot(req,snapshot); self.emit("task.done", req)
            elif kind == "task.import":
                self.snapshot(req, self.tasks.import_copy(req.get("source_directory")))
                self.emit("task.done", req)
            elif kind == "task.save":
                self.snapshot(req, self.tasks.save()); self.emit("task.done", req)
            elif kind == "task.export":
                format = req.get("format", "txt")
                raw = (self.tasks.export_audio(format) if self.tasks.task["kind"]=="音频" else self.tasks.export()).encode()
                name = uuid.uuid4().hex + "." + (format if self.tasks.task["kind"]=="音频" else "txt")
                atomic_write(raw, self.tasks.store.root / "exports" / name)
                self.emit("task.exported", req, filename=name, byte_count=len(raw), sha256=sha(raw))
            else: self.error(req, "unsupported_operation")
        except (CoreError, OSError, TypeError, ValueError) as exc:
            if self.tasks.save_blocked and self.tasks.task:self.snapshot(req,self.tasks.snapshot())
            self.error(req, getattr(exc, "code", "task_io_error"), str(exc))

    def start_text(self, req):
        op_id, lease_id = identifier(req.get("operation_id")), identifier(req.get("lease_id"))
        lease_ms = req.get("lease_ms", 20000)
        if type(lease_ms) is not int or not 100 <= lease_ms <= 30000:
            raise CoreError("操作租约超出有界范围。")
        if req["type"] in ("capture.recover", "audio.asr", "audio.refine", "audio.play", "document.extract", "document.ocr", "document.render", "image.ocr", "image.render", "task.start"):
            task = self.tasks.task
            if not task or (req.get("task_id"), req.get("revision")) != (task["id"], task["revision"]):
                raise CoreError("请求不属于当前任务修订。", "request_revision_mismatch")
            if req["type"] in ("document.render","image.render"):
                if task["kind"] != ("图片" if req["type"]=="image.render" else "文档"): raise CoreError("当前不是文档任务。", "task_kind_mismatch")
                from translator_core.images import content, units
                page = next((p for p in units(content(task)) if p['id']==req.get('page')),None)
                if not page or page["revision"] != req.get("page_revision"):
                    raise CoreError("来源回看请求不属于当前页修订。", "page_revision_mismatch")
        if req['type']=='task.start':
            task=self.tasks.task
            if task and self.tasks.store.history.metadata(task['id'],task['kind'])['trashed_at'] is not None:
                raise CoreError('此记录在回收站，请先恢复后再继续。','task_trashed')
        with self.operation_lock:
            if self.operation is not None: raise CoreError("已有活动操作。", "busy")
            op = dict(id=op_id, lease=lease_id, cancel=threading.Event(), lease_ms=lease_ms,
                      deadline=time.monotonic() + lease_ms / 1000, expired=False,
                      ollama_discovery=self.ollama_discovery)
            if req['type'] in WORKBENCH_OPERATIONS or req['type'] in ('capture.start','audio.create','service.check') and self.workbench:
                op['run_cancel']=threading.Event()
                op['can_stop']=req['type']=='workbench.translate' and req.get('mode')=='document'
            if req['type']=='managed.operate':
                selected = self.resources.selection
                op['managed_owner'] = self.managed.owner(self.session_id, req['request_id'], op_id,
                    self.preferences.revision, self.resources.revision,
                    selected['resource_set_id'] if selected else None)
            if req['type']=='resource.check':
                op['resource_owner'] = self.resources.owner(self.session_id,req['request_id'],op_id)
            self.operation = op
            self.emit("operation.started", req, operation_id=op_id, lease_id=lease_id,
                      lease_ms=lease_ms, result_kind=req["type"], task_id=req.get("task_id"), revision=req.get("revision"))
            self.thread = threading.Thread(target=self.run_text, args=(req, op), daemon=False)
            self.thread.start()

    def run_text(self, req, op):
        ended = threading.Event()
        engine = None
        def watch_lease():
            while not ended.wait(.05):
                if time.monotonic() >= op["deadline"]:
                    with self.operation_lock:
                        if op.get('committed') or ended.is_set():
                            return
                        if time.monotonic() < op['deadline']:
                            continue
                        op["expired"] = True
                        op["cancel"].set()
                        if op.get('run_cancel'):
                            op['run_cancel'].set()
                    if op.get('engine'): op['engine'].cancel()
                    if engine: engine.cancel()
                    if self.tasks: self.tasks.cancel(op["id"])
                    return
        watcher = threading.Thread(target=watch_lease, daemon=True); watcher.start()
        status, detail = "failed", None
        try:
            if req['type'] in WORKBENCH_OPERATIONS:
                status,detail=self.run_workbench(req,op)
            elif req['type'].startswith('environment.'):
                detail = self.apply_environment(req, op)
                status = 'completed'
            elif req['type'] == 'managed.operate':
                drained = []
                previous_revision = self.managed.revision
                def before_change(component):
                    self.drain_managed(component)
                    drained.append(component)
                last_progress = [0.0]
                def managed_progress(stage, completed, total, byte_count):
                    now = time.monotonic()
                    if now-last_progress[0] >= .2:
                        last_progress[0] = now
                        self.emit('managed.progress', req, operation_id=op['id'], lease_id=op['lease'],
                            owner=op['managed_owner'], stage=stage, completed=completed, total=total, byte_count=byte_count)
                try:
                    detail = self.managed.operate(req['action'], req.get('component'), op['managed_owner'],
                        op['cancel'], managed_progress, instance=req.get('instance'), confirmed=req.get('confirmed',False),
                        protected_roots=self.protected_resources(), before_change=before_change)
                finally:
                    if drained or self.managed.revision != previous_revision:
                        self.synchronize_managed(op['cancel'])
                status = detail['transaction']['stage']
                if status == 'completed':
                    detail['resources'] = self.resources.describe()
                    detail['dictionary'] = self.dictionary.description()
            elif req['type'] == 'resource.check':
                last_progress = [0.0]
                def progress(resource_id, completed, total, byte_count):
                    now=time.monotonic()
                    if now-last_progress[0] >= .2:
                        last_progress[0]=now
                        self.emit('resource.progress',req,operation_id=op['id'],lease_id=op['lease'],
                            owner=op['resource_owner'],resource_id=resource_id,completed=completed,total=total,byte_count=byte_count)
                description=self.resources.check(op['resource_owner'],op['cancel'],progress,runtime=req['runtime'])
                detail=dict(owner=op['resource_owner'],description=description)
                status=description['attempt']['status']
            elif req["type"] == "service.check":
                values = self.tasks.task_values() if self.preferences else None
                endpoint = self.endpoint_override or (values['endpoint'] if values else self.endpoint)
                direction = req.get("direction", "en-zh")
                recipe = recipe_for(values, direction) if values else self.tasks.new_recipe(direction)
                if values and values['translation_backend'] == 'application_ollama':
                    if self.endpoint_override is not None:
                        raise CoreError('本应用后端需要专用服务，请清除命令行外部端点覆盖。', 'backend_override')
                    endpoint = self.service.ensure(op['cancel'], recipe['model'])
                engine = Ollama(endpoint, cancel=op["cancel"], require_mac_version=bool(self.local_package))
                op["engine"] = engine
                detail = engine.verify(recipe)
                if self.resources:
                    self.resources.service_evidence=dict(recipe=recipe,
                        state='service_identity_verified',endpoint_probed=True,endpoint=endpoint,
                        checked_at=time.time(),result=detail,model_load='not_tested',inference='not_tested',
                        meaning='沿用service.check的协议/模型身份响应；不表示生成或语音业务通过')
                status = "completed"
            elif req["type"] == "audio.select":
                detail=self.tasks.select_audio(req.get("source_path"),op["id"],op["cancel"]); status="completed"
            elif req['type']=='audio.create':
                setup = self.audio_setup(req,op)
                if op['cancel'].is_set():
                    raise Cancelled()
                self.snapshot(req,self.tasks.create_audio(req.get('selection_token'),setup),op)
                status = 'completed'
            elif req['type'] == 'capture.start':
                setup = self.audio_setup(req,op,live=True) if self.workbench else None
                status = self.tasks.run_capture(op['id'], op['lease'],
                    dict(worker=self.session_id, request=req['request_id'], operation=op['id'], lease=op['lease']),
                    req.get('source_kind'), req.get('device_uid'), req.get('selection_generation'), op['cancel'],
                    lambda snapshot: self.snapshot(req, snapshot, op),setup=setup)
            elif req['type'] == 'capture.recover':
                status = self.tasks.recover_recording(op['id'], op['cancel'], lambda snapshot: self.snapshot(req, snapshot, op))
            elif req["type"] in ("audio.asr", "audio.refine"):
                task = self.tasks.task
                variant = task['audio'].get('asrModel') if self.workbench and task and task['kind']=='音频' else None
                verified = None
                if self.speech and not variant:
                    # Older audio tasks used Whisper large-v3-turbo and defaulted to English.
                    from translator_core.resource_macos import LEGACY_WHISPER
                    variant = LEGACY_WHISPER
                if variant:
                    from translator_core.languages import speech_language
                    capability = 'speech.refine' if req['type']=='audio.refine' else 'speech.file_asr'
                    language = speech_language(variant,task['configuration']['direction'])
                    binding = self.audio_resource_binding(req,op,variant,language,capability)
                    if self.speech:
                        selection, defaults = self.speech_defaults()
                        specs = self.speech.specs(variant, defaults, selection)
                        verified = {capability: binding, 'audio.decode': self.speech.binding(variant, language,
                            'audio.decode', specs, dict(checked_at=binding['checked_at'], receipt=binding['check']))}
                status=self.tasks.recognize_audio(op["id"],op["cancel"],
                    lambda snapshot:self.snapshot(req,snapshot,op),req["type"]=="audio.refine", verified=verified)
            elif req["type"] == "audio.play":
                status=self.tasks.play_audio(op["id"],op["cancel"],lambda value,folder:self.emit("audio.playback",req,operation_id=op["id"],lease_id=op["lease"],task_id=req["task_id"],pcm=self.tasks.task["audio"]["pcm"]["fingerprint"],playback=value),req.get('start_sample',0))
            elif req['type'] == 'image.select':
                detail=self.tasks.select_image(req.get('source_path'),op['id'],op['cancel'],
                    origin=req.get('origin','file-original'),fingerprint=req.get('sha256'),byte_count=req.get('byte_count'))
                status='completed'
            elif req["type"] == "document.select":
                path = req.get("source_path")
                if not isinstance(path, str) or len(path) > 4096: raise CoreError("来源路径无效。")
                detail = self.tasks.select_document(path, op["id"], op["cancel"]); status = "completed"
            elif req["type"] in ("document.extract", "document.ocr", "image.ocr"):
                status = self.tasks.extract(op["id"], op["cancel"], lambda snapshot: self.snapshot(req, snapshot, op),mode='ocr' if req['type'] in ('document.ocr','image.ocr') else 'text')
            elif req['type']=='image.render':
                detail=self.tasks.render_image(op['cancel'])
                detail.update(task_id=req['task_id'],revision=req['revision'],page_revision=req['page_revision'])
                status='completed'
            elif req['type']=='image.render_selection':
                detail=self.tasks.render_selection(req.get('selection_token'),op['cancel']);status='completed'
            elif req["type"] == "document.render":
                detail = self.tasks.render(req.get("page"), op["cancel"])
                detail.update(task_id=req["task_id"], revision=req["revision"], page_revision=req["page_revision"])
                status = "completed"
            else:
                status = self.tasks.run(op["id"], op["cancel"], lambda snapshot: self.snapshot(req, snapshot, op))
        except Exception as exc:
            from translator_core import Cancelled
            code = getattr(exc, 'code', type(exc).__name__)
            environment = req['type'].startswith('environment.')
            cancelled = isinstance(exc, Cancelled) or (environment or req['type'] in WORKBENCH_OPERATIONS) and (code == 'check_cancelled' or op['cancel'].is_set())
            status = 'cancelled' if cancelled else 'failed'
            detail = {"error": str(exc), "code": code}
            if environment and self.resources.dirty:
                detail['resources'] = self.resources.describe()
            if req['type']=='managed.operate':
                detail.update(owner=op['managed_owner'], description=self.managed.describe())
            if req['type']=='resource.check':
                self.resources.attempt=dict(owner=op['resource_owner'],status='failed',checked_at=time.time(),error=translate_error(exc))
                detail.update(owner=op['resource_owner'],description=self.resources.describe())
        finally:
            ended.set(); watcher.join(timeout=1)
            if req["type"] in ("capture.start", "capture.recover", "audio.asr", "audio.refine", "task.start", "document.extract", "document.ocr", "image.ocr"): self.snapshot(req, self.tasks.snapshot(), op)
            with self.operation_lock:
                capture = (self.tasks.task.get('audio') or {}).get('recording') if self.tasks and self.tasks.task else None
                terminal = capture.get('terminal', {}) if capture and req['type'] == 'capture.start' else {}
                drained = terminal.get('asrThreadEnded', True) and terminal.get('translationThreadEnded', True)
                self.emit("operation.finished", req, operation_id=op["id"], lease_id=op["lease"],
                          status="lease_expired" if op["expired"] else status, tail_drained=drained,
                          result={"kind": {"capture.start":"capture-session", "capture.recover":"capture-recovery", "audio.select":"audio-selection", "audio.asr":"audio-asr", "audio.refine":"audio-refine", "audio.play":"audio-play", "resource.check":"resource-check", "managed.operate":"managed-resource-operation", "task.start":"text-task", "service.check":"service-diagnostic", "document.select":"document-selection", "document.extract":"document-extraction", "document.ocr":"document-ocr", "document.render":"document-render", "image.select":"image-selection", "image.ocr":"image-ocr", "image.render":"image-render",'image.render_selection':'image-selection-render',
                                           'environment.download':'environment-download', 'environment.dictionary':'dictionary-selection',
                                           'environment.speech_folder':'speech-folder-selection',
                                           'environment.runtime_zip':'whisper-runtime-import'}.get(req["type"],req["type"]), "detail": detail})
                self.last_finished = (op["id"], op["lease"]); self.operation = None


def initialize_worker(args):
    identity_path=Path(__file__).resolve().parents[1]/'build-identity.json'
    package = identity_path.parent
    identity = json.loads(identity_path.read_bytes()) if identity_path.is_file() else {}
    local_package = (package, identity) if identity.get('delivery_layout') == LOCAL_DELIVERY else None
    if identity:
        if identity.get('preview_id')!=PREVIEW_ID:raise CoreError('包身份与worker不匹配。','package_identity')
        args.dictionary_config=args.dictionary_config or identity.get('dictionary_config')
        bundled_catalog = package/'install-catalog.json'
        if local_package:
            if (args.install_catalog and Path(args.install_catalog) != bundled_catalog) or (
                args.catalog_sha256 and args.catalog_sha256 != identity['install_catalog_sha256']
            ):
                raise CoreError('本地交付版使用包内冻结安装清单。', 'package_identity')
            args.install_catalog = str(bundled_catalog)
        elif args.install_catalog is None and bundled_catalog.is_file():
            args.install_catalog = str(bundled_catalog)
        args.catalog_sha256=args.catalog_sha256 or identity.get('install_catalog_sha256')
        args.translation_models_root=args.translation_models_root or identity.get('translation_models_root')
        platform_folder=package/'shared/platform/macos'
        args.boundary_helper=args.boundary_helper or str(platform_folder/'text-boundaries')
        args.pdf_helper=args.pdf_helper or str(platform_folder/'pdf-facts')
        args.image_helper=args.image_helper or str(platform_folder/'image-facts')
    project = Path(identity.get('project_root', package.parents[1]))
    root = data_directory(project, identity, args.preview_data_root, args.task_root)
    temporary = no_links(root/'temporary')
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary), XDG_CACHE_HOME=str(temporary/'cache'))
    if identity and not local_package:
        initialize_package_resources(package, identity, root)
    return Worker(identifier(args.session_id), sys.stdout.buffer, str(root/'tasks'), args.boundary_helper,
                  args.endpoint, args.pdf_helper, args.pdf_library, args.dictionary_config, args.image_helper,
                  root, args.install_catalog, args.catalog_sha256, args.translation_models_root,
                  local_package=local_package)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task-root")
    parser.add_argument("--boundary-helper")
    parser.add_argument("--endpoint")
    parser.add_argument("--pdf-helper")
    parser.add_argument("--pdf-library")
    parser.add_argument("--image-helper")
    parser.add_argument("--dictionary-config")
    parser.add_argument("--preview-data-root")
    parser.add_argument('--install-catalog')
    parser.add_argument('--catalog-sha256')
    parser.add_argument('--translation-models-root')
    args = parser.parse_args(argv)
    try:
        worker = initialize_worker(args)
    except (CoreError, OSError) as exc:
        code = ('worker_data_permission' if isinstance(exc, PermissionError) else 'worker_startup_io')
        detail = {"code": getattr(exc, "code", code), "message": str(exc)[:240]}
        print(STARTUP_ERROR_PREFIX + json.dumps(detail, separators=(",", ":")), file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    try:
        while raw := sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1):
            try:
                req = decode(raw)
            except ProtocolError as exc:
                print(f"B01 protocol rejected: {exc}", file=sys.stderr, flush=True)
                raise SystemExit(2) from exc
            if not worker.handle(req):
                break
    finally:
        worker.drain()
        if worker.tasks: worker.tasks.store.close()
