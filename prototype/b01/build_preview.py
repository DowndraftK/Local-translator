"""Native development candidate build, explicitly separate from the old Swift App."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import plistlib
import py_compile
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def assemble_install_sources(catalog_path, notices, target):
    """Copy the offline dictionaries and their notices into the signed bundle."""
    from translator_core.resource_lifecycle import validate_catalog
    catalog = validate_catalog(json.loads(catalog_path.read_bytes()))
    catalog['components'] = [item for item in catalog['components'] if item['id'] == 'dictionary']
    validate_catalog(catalog)
    manifests = target/'manifests'
    manifests.mkdir(parents=True)
    legal = target/'legal'
    legal.mkdir()
    shutil.copy2(Path(__file__).with_name('mac-local-notices.md'), legal/'RESOURCE-LICENSES.md')
    for name in ('dictionary', 'python', 'qt-source-notices'):
        shutil.copytree(notices/name, legal/name, symlinks=True)
    for item in catalog['components']:
        spec = item['resource']
        old_root = Path(spec['root'])
        manifest = Path(spec['manifest']['path'])
        if manifest.stat().st_size != spec['manifest']['bytes'] or hashlib.sha256(manifest.read_bytes()).hexdigest() != spec['manifest']['sha256']:
            raise ValueError(f"Install manifest differs from locked identity: {item['id']}")
        new_root = target/item['id']
        shutil.copytree(old_root, new_root, symlinks=True)
        if manifest.is_relative_to(old_root):
            new_manifest = new_root/manifest.relative_to(old_root)
        else:
            new_manifest = manifests/(item['id']+'.json')
            shutil.copy2(manifest, new_manifest)
        spec['root'] = str(new_root.relative_to(target))
        spec['manifest']['path'] = str(new_manifest.relative_to(target))
        item['license_materials'] = ['legal/RESOURCE-LICENSES.md', 'legal/dictionary']
    return catalog


def assemble_whisper(reference, model, notices, target):
    """Copy the verified Mac speech runtime, English weights and audio assets."""
    from translator_core.resource_lifecycle import copy_manifest_resource
    from translator_core.resource_macos import DEFAULT_WHISPER, trusted_manifest, whisper_variant
    from translator_core.resources import verify_resource

    specs = {spec['resource_id']: spec for spec in reference['resources']}
    small = specs['speech.model']
    if whisper_variant(small['manifest']['sha256']) != DEFAULT_WHISPER:
        raise ValueError('The bundled speech model must be the pinned Whisper small.en.')
    if not all(trusted_manifest(name, specs[name]['manifest']['sha256'])
               for name in ('speech.runtime', 'speech.backend', 'speech.model', 'speech.assets')):
        raise ValueError('The submitted speech references do not match the retained component pins.')
    small = dict(small, root=str(model))
    target.mkdir()
    roots = {'speech.runtime': target/'runtime',
             'speech.model': target/'small.en',
             'speech.assets': target/'assets'}
    cancel = threading.Event()
    deadline = time.monotonic() + 300
    for spec in (specs['speech.runtime'], small, specs['speech.assets']):
        destination = roots[spec['resource_id']]
        copy_manifest_resource(spec, destination, cancel, deadline, lambda *_: None)
        destination.chmod(0o755)
        for directory in destination.rglob('*'):
            if not directory.is_symlink() and directory.is_dir():
                directory.chmod(0o755)
    roots['speech.backend'] = roots['speech.runtime']
    verify_resource(dict(specs['speech.backend'], root=str(roots['speech.backend'])), cancel, deadline,
                    lambda *_: None)

    licenses = target/'Model-Licenses'
    licenses.mkdir()
    shutil.copy2(model/'README.md', licenses/'Whisper-small.en-model-card.md')
    shutil.copy2(model/'UPSTREAM-OPENAI-LICENSE.txt', licenses/'OpenAI-Whisper-MIT.txt')
    shutil.copy2(notices/'qt-source-notices/qtscxml/LICENSES/Apache-2.0.txt',
                 licenses/'Apache-2.0.txt')
    return roots


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument('--dictionary-config',type=Path)
    parser.add_argument('--preview-data-root',type=Path)
    parser.add_argument('--resource-reference',type=Path)
    parser.add_argument('--gui-runtime',type=Path)
    parser.add_argument('--install-catalog',type=Path)
    parser.add_argument('--translation-models-root',type=Path)
    parser.add_argument('--mac-local-delivery-notices', type=Path,
                        help='Build the Mac local delivery layout using notices from the frozen assembly')
    parser.add_argument('--bundled-whisper-model', type=Path,
                        help='Directory with the verified Whisper small.en weights and upstream notices')
    parser.add_argument('--module-cache-path',type=Path,help='Explicit owned, rebuildable compile cache; never an old evidence cache')
    parser.add_argument("--pdf-library", type=Path, help="Pinned wheel unpacked under a NEW artifacts directory on Windows")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    project = source.parents[1]
    lock_path = source / "dependencies.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(project / "dist"):
        parser.error("Use a NEW candidate directory under this repository's dist directory.")
    if not Path(sys.prefix).resolve().is_relative_to(project / "artifacts") or sys.prefix == sys.base_prefix:
        parser.error("Build using a separate project-local B01 development venv.")
    if platform.python_version() != "3.12.14":
        parser.error("Use pinned Python 3.12.14.")
    branch = run("git", "-C", project, "branch", "--show-current")
    if not branch.startswith("codex/"):
        parser.error("Build from the B01 continuation feature branch, not main.")
    for package in lock["packages"]:
        if importlib.metadata.version(package["name"]) != package["version"]:
            parser.error("Qt development dependency mismatch.")
    if sys.platform not in ("darwin", "win32"):
        parser.error("Build on native macOS/Windows only.")
    from PySide6.QtCore import qVersion
    sys.path[:0] = [str(source), str(project / "shared")]
    from translator_preview import PREVIEW_ID, PRODUCT_VERSION, DEFAULT_ENDPOINT
    from translator_preview.runtime import owned_python
    from translator_preview.paths import LOCAL_DELIVERY
    from translator_core import CORE_VERSION
    from translator_preview.protocol import PROTOCOL
    stage = PREVIEW_ID.split("-", 1)[0].upper()
    local_delivery = args.mac_local_delivery_notices is not None
    product_version = PRODUCT_VERSION if local_delivery else '0.2.11'
    if local_delivery and (sys.platform != 'darwin' or stage != 'T09B'):
        parser.error('The local delivery layout requires the T09B Mac runtime.')
    if local_delivery and not args.bundled_whisper_model:
        parser.error('The Mac delivery requires a verified --bundled-whisper-model directory.')
    if args.bundled_whisper_model and not local_delivery:
        parser.error('The bundled speech payload belongs to the Mac local delivery layout.')
    if stage not in ('T01','T02','T03A','T11D','T06A','T04','T05','T09A','T07','T08','T06B','T09B'):
        parser.error('Unknown preview stage; no fallback to an old data root.')
    preview_data = project / "artifacts" / {"T03A": "t03a-20261022", "T02": "t02-20261021",
        "T01": "t01-20261020"}.get(stage, f"{stage.lower()}-20261005")
    if stage=='T11D': preview_data=project/'artifacts/t11d-20261023-run1/candidate-data'
    if stage=='T06A': preview_data=project/'artifacts/t06a-20261023-run1/candidate-data'
    if stage in ('T04','T05','T09A','T07','T08','T06B','T09B'):
        if not args.preview_data_root or not args.dictionary_config:
            parser.error(stage+' requires explicit --preview-data-root and read-only --dictionary-config.')
        preview_data=args.preview_data_root.resolve()
    if args.preview_data_root:
        preview_data=args.preview_data_root.resolve()
        if not preview_data.is_relative_to(project/'artifacts'):
            parser.error('Use an isolated preview data root under project artifacts.')
        if stage=='T07' and not preview_data.relative_to(project/'artifacts').parts[0].startswith('t07-20261109-run2'):
            parser.error('T07 requires its registered new data root.')
        if stage in ('T08','T06B','T09B'):
            execution_root = project/'artifacts'/preview_data.relative_to(project/'artifacts').parts[0]
            run_prefix = 't10-mac-' if local_delivery else stage.lower()+'-'
            registration = execution_root/('plans/registration.json' if local_delivery else 'registration.json')
            if not execution_root.name.startswith(run_prefix) or execution_root.name.startswith(run_prefix+'prompt-') or not registration.is_file():
                parser.error(stage+' requires its independent registered execution root.')
        if stage=='T05' and (not preview_data.relative_to(project/'artifacts').parts[0].startswith('t05-') or
                            preview_data.relative_to(project/'artifacts').parts[0].startswith('t05-prompt-')):
            parser.error('T05 preview data must use a new registered T05 run root.')
        if stage=='T09A' and (not preview_data.relative_to(project/'artifacts').parts[0].startswith('t09a-') or
                             preview_data.relative_to(project/'artifacts').parts[0].startswith('t09a-prompt-')):
            parser.error('T09-A preview data must use a new registered T09-A run root.')
    if stage in ('T09A','T07','T08','T06B','T09B'):
        if not args.resource_reference: parser.error('T09-A requires an explicit read-only resource reference.')
        from translator_core.resources import ResourceManager, CONTRACT
        manager=ResourceManager(preview_data)
        if manager.config.exists(): parser.error('Use a new candidate data directory, preserving old selection.')
        manager.select(args.resource_reference.absolute()); manager.save()
    dictionary_config=args.dictionary_config.resolve() if args.dictionary_config else None
    if dictionary_config and not dictionary_config.is_file(): parser.error('Explicit dictionary config missing.')
    if qVersion() != lock["qt"]:
        parser.error("Loaded Qt does not match lock.")
    module_cache = args.module_cache_path.resolve() if args.module_cache_path else output / 'module-cache'
    cache_root = execution_root if stage in ('T08','T06B','T09B') else project/'artifacts'/('t07-20261109-run2' if stage=='T07' else 't09a-20261106-run1' if stage=='T09A' else 't05-20261105-run1' if stage=='T05' else 't04-20261103-run1')
    if args.module_cache_path and not module_cache.is_relative_to(cache_root):
        parser.error('Explicit compile cache must be inside the registered package root.')
    if stage=='T09B' and (not args.gui_runtime or not args.install_catalog or
                         not local_delivery and not args.translation_models_root):
        parser.error('T09-B requires frozen GUI runtime and install catalog. The development layout also needs its models root.')
    output.mkdir(parents=True)
    mac_build = None
    if sys.platform == "darwin":
        existing = [46]
        for marker in (project / "dist").glob("*/build-number.txt"):
            try: existing.append(int(marker.read_text()))
            except (OSError, ValueError): pass
        for info in (project / "dist").glob("*/**/Contents/Info.plist"):
            try:
                data = plistlib.loads(info.read_bytes())
                if data.get("CFBundleIdentifier", "").startswith("local.kevin.translator"):
                    existing.append(int(data["CFBundleVersion"]))
            except (OSError, ValueError, KeyError): pass
        mac_build = max(existing) + 1
        (output / "build-number.txt").write_text(f"{mac_build}\n")
        build_id = f"macos-arm64-build{mac_build}"
        app = output / ("本地翻译器.app" if local_delivery else f"本地翻译器 {stage}预览.app")
        package = app / "Contents/Resources/B01"
        (app / "Contents/MacOS").mkdir(parents=True)
    else:
        if platform.machine().lower() not in ("amd64", "x86_64"):
            parser.error("Requires Windows x64.")
        # Windows identity is independent of the historical Mac build sequence.
        build_id = "windows-x64-" + stage.lower() + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        package = output / "B01"
    shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(project / "shared", package / "shared", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if stage=='T09B':
        runtime = app/'Contents/Resources/Runtime/gui'
        shutil.copytree(args.gui_runtime.resolve(),runtime,symlinks=True)
        install_sources = runtime.parent/'install-sources'
        if local_delivery:
            catalog = assemble_install_sources(args.install_catalog, args.mac_local_delivery_notices, install_sources)
            (package/'install-catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        else:
            shutil.copy2(args.install_catalog,package/'install-catalog.json')
        reference = json.loads(args.resource_reference.read_bytes())
        speech_roots = assemble_whisper(reference, args.bundled_whisper_model.resolve(),
                                        args.mac_local_delivery_notices, runtime.parent/'whisper') if local_delivery else {}
        manifests = package/'resource-manifests'
        manifests.mkdir()
        package_paths = {}
        for spec in reference['resources']:
            name = spec['resource_id']
            if name=='gui.runtime':
                root=runtime
                original_manifest=Path(spec['manifest']['path'])
                target=runtime/original_manifest.relative_to(Path(spec['root'])) if original_manifest.is_relative_to(Path(spec['root'])) else manifests/'gui.json'
                if not target.is_file(): shutil.copy2(original_manifest,target)
            elif name=='speech.bridge':
                root=package/'runtime'
                shutil.copytree(spec['root'],root,symlinks=True)
                target=manifests/'bridge.json'
                if local_delivery:
                    from translator_core.resource_macos import MULTILINGUAL_BRIDGE
                    from translator_core.resources import encoded
                    previous = json.loads(Path(spec['manifest']['path']).read_bytes())
                    files = {}
                    for relative in previous['files']:
                        body = (root/relative).read_bytes()
                        files[relative] = dict(size=len(body), sha256=hashlib.sha256(body).hexdigest())
                    manifest = encoded(dict(previous, files=files))
                    if hashlib.sha256(manifest).hexdigest() != MULTILINGUAL_BRIDGE:
                        parser.error('Bundled multilingual speech bridge differs from its pinned manifest.')
                    target.write_bytes(manifest)
                    spec['manifest'].update(bytes=len(manifest), sha256=MULTILINGUAL_BRIDGE)
                    spec['version'] = 'whisper-multilingual-bridge-v1'
                else:
                    shutil.copy2(spec['manifest']['path'],target)
            elif name=='mac.frameworks':
                root=package/'shared/platform/macos'
                shutil.copy2(Path(spec['root'])/'resource-facts',root/'resource-facts')
                target=manifests/'frameworks.json'
                shutil.copy2(spec['manifest']['path'],target)
            elif name in speech_roots:
                root = speech_roots[name]
                if name=='speech.runtime':
                    target = root/'component.json'
                else:
                    target = manifests/(name+'.json')
                    shutil.copy2(spec['manifest']['path'], target)
                spec['distribution_status'] = 'local-personal-use-only'
            elif name in ('speech.model','speech.assets','speech.backend'):
                root = Path(spec['root'])
                target=manifests/(name+'.json')
                shutil.copy2(spec['manifest']['path'],target)
            else:
                continue
            spec['root']=str(root)
            spec['manifest']['path']=str(target)
            if name in ('gui.runtime','speech.bridge','mac.frameworks') or name in speech_roots:
                package_paths[name]=dict(root=str(Path(__import__('os').path.relpath(root,package))),
                                         manifest=str(Path(__import__('os').path.relpath(target,package))))
            else:
                package_paths[name]=dict(root=str(root),manifest=str(target.relative_to(package)))
        (package/'resource-reference-template.json').write_text(json.dumps(reference,ensure_ascii=False))
    assets = package / "shared/translator_core/assets"; assets.mkdir()
    for recipe in (project / "fixtures/b01").glob("swift-recipe-*.json"):
        shutil.copy2(recipe, assets / recipe.name)
    if sys.platform == "darwin":
        helper = package / "shared/platform/macos/text-boundaries"
        subprocess.run(["swiftc", "-O", "-module-cache-path", str(module_cache),
                        str(project / "shared/platform/macos/TextBoundaries.swift"), "-o",
                        str(helper)], check=True)
        subprocess.run(["codesign", "--sign", "-", str(helper)], check=True)
        pdf_helper = package / "shared/platform/macos/pdf-facts"
        subprocess.run(["swiftc", "-O", "-module-cache-path", str(module_cache),
                        str(project / "shared/platform/macos/PDFFacts.swift"), "-o", str(pdf_helper)], check=True)
        subprocess.run(["codesign", "--sign", "-", str(pdf_helper)], check=True)
        image_helper=package/'shared/platform/macos/image-facts'
        if stage in ('T05','T09A','T07','T08','T06B','T09B'):
            subprocess.run(['swiftc','-O','-module-cache-path',str(module_cache),
                str(project/'shared/platform/macos/ImageFacts.swift'),'-o',str(image_helper)],check=True)
            subprocess.run(['codesign','--sign','-',str(image_helper)],check=True)
    elif args.pdf_library:
        library = args.pdf_library.resolve()
        if not library.is_relative_to(project / "artifacts"):
            parser.error("Use the fixed PDF software library under this project artifacts root.")
        pdf_lock = json.loads((source / "pdf-dependencies.lock.json").read_text(encoding="utf-8"))
        package_lock = pdf_lock["packages"][0]
        wheel = library.parent / package_lock["filename"]
        if wheel.stat().st_size != package_lock["bytes"] or hashlib.sha256(wheel.read_bytes()).hexdigest() != package_lock["sha256"]:
            parser.error("Official PDF wheel hash/size mismatch.")
        import zipfile
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.endswith("/"): continue
                path = library / name
                if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(archive.read(name)).digest():
                    parser.error("Unpacked PDF library differs from the official locked wheel.")
        shutil.copytree(library, package / "vendor/pdfium", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if stage in ('T07','T08','T06B','T09B') and sys.platform=='darwin':
        playback=package/'shared/platform/macos/audio-playback'
        subprocess.run(['swiftc','-O','-module-cache-path',str(module_cache),str(project/'shared/platform/macos/AudioPlayback.swift'),'-o',str(playback)],check=True)
        subprocess.run(['codesign','--sign','-',str(playback)],check=True)
    if stage in ('T08','T06B','T09B') and sys.platform == 'darwin':
        capture_app = package / 'shared/platform/macos/AudioCapture.app'
        capture = capture_app / 'Contents/MacOS/AudioCapture'
        capture.parent.mkdir(parents=True)
        converter = project / 'Sources/TranslatorCore/PCM16StreamConverter.swift'
        shutil.copy2(converter, package / 'shared/platform/macos/PCM16StreamConverter.swift')
        subprocess.run(['swiftc', '-swift-version', '5', '-parse-as-library', '-O', '-module-cache-path', str(module_cache),
            str(converter), str(project / 'shared/platform/macos/AudioCapture.swift'), '-o', str(capture)], check=True)
        capture_info = dict(CFBundleIdentifier='local.kevin.translator.'+stage.lower()+'.capture', CFBundleName='本地翻译器 '+stage+' 音频采集',
            CFBundleExecutable='AudioCapture', CFBundlePackageType='APPL', CFBundleVersion=str(mac_build),
            CFBundleShortVersionString=product_version, LSUIElement=True, LSMinimumSystemVersion='26.0',
            NSMicrophoneUsageDescription='在你主动开始麦克风同传后，接收选定麦克风的声音并保存本地录音。',
            NSScreenCaptureUsageDescription='在你主动开始电脑声音同传后，直接接收系统播放音频。只保存音频。')
        (capture_app / 'Contents/Info.plist').write_bytes(plistlib.dumps(capture_info))
        subprocess.run(['codesign', '--sign', '-', str(capture_app)], check=True)
    files = sorted(p for root in (source, project / "shared") for p in root.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts)
    files += sorted((project / "fixtures/b01").glob("swift-recipe-*.json"))
    if stage in ('T08','T06B','T09B'): files.append(project / 'Sources/TranslatorCore/PCM16StreamConverter.swift')
    if stage=='T09B':
        files += sorted(p for p in (project/'runtime').rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    identity = {"schema": 1, "product_version": product_version, "preview_id": PREVIEW_ID, 'project_root':str(project),
        "build_id": build_id, "mac_build": mac_build,
        "native_platform": sys.platform, "machine": platform.machine(),
        "built_at": datetime.now(timezone.utc).isoformat(), "branch": branch,
        "history_metadata": "task-history-metadata-v1", "history_query": "task-history-query-v2", "history_locator": "task-history-locator-v1",
        "source_commit": run("git", "-C", project, "rev-parse", "HEAD"),
        "working_tree_modified": bool(run("git", "-C", project, "status", "--porcelain")),
        "source_files": {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "python_version": platform.python_version(), "python_executable": sys.executable,
        "rebuildable_module_cache": str(module_cache),
        "qt_version": qVersion(), "protocol": PROTOCOL, "shared_core": CORE_VERSION,
        "worker_executable": owned_python(),
        "preview_task_root": str(preview_data / "tasks"),
        "preview_data_root":str(preview_data) if stage in ('T09A','T07','T08','T06B','T09B') else None,
        "preview_endpoint": DEFAULT_ENDPOINT,
        "preview_preferences_path": str(preview_data / "preferences/appearance.json") if stage in ("T01", "T02", "T03A", "T11D", "T06A", "T04",'T05','T09A','T07','T08','T06B','T09B') else None,
        "dictionary_config": str(dictionary_config) if dictionary_config else None,
        "dependency_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "dependencies": {item["name"]: item["version"] for item in lock["packages"]},
        "standalone_runtime": False, "distribution": "local-development-preview-only"}
    from dataclasses import asdict
    from translator_core.text_policy import policy_identity
    from translator_core.dictionary.rules import identity as dictionary_rules
    identity['text_policy']=asdict(policy_identity())
    identity['dictionary_rules']=dictionary_rules()
    identity['dictionary_resources_external_only'] = not local_delivery
    if sys.platform == "darwin":
        identity["platform_adapter"] = {"name": "Foundation-Character-NaturalLanguage-boundaries-v1",
                                       "binary_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                                       "compiler": run("swiftc", "--version")}
    else:
        identity["platform_adapter"] = {"name": "Qt-QTextBoundaryFinder-6.10.2",
                                       "splitter_version": "qt6102-unicode-utf8-v1"}
    from translator_core.pdf_adapter import platform_pdf_adapter
    facts = preview_data / 'build-facts' if stage in ('T08','T06B','T09B') else output
    facts.mkdir(parents=True, exist_ok=True)
    pdf_probe = platform_pdf_adapter(facts / "fact-probe", pdf_helper if sys.platform == "darwin" else None,
                                     owned_python(), package / "vendor/pdfium" if sys.platform == "win32" else None)
    identity["pdf_adapter"] = {"identity": pdf_probe.identity, "actual_probe_confirmed": pdf_probe.verified,
                               "render_max_edge": 3000, "render_max_pixels": 9000000,
                               "library_lock_sha256": hashlib.sha256((source / "pdf-dependencies.lock.json").read_bytes()).hexdigest()}
    from translator_core.ocr_adapter import platform_ocr_adapter
    ocr_probe = platform_ocr_adapter(facts / 'ocr-probe', pdf_probe, pdf_helper if sys.platform=='darwin' else None)
    identity['ocr_adapter'] = dict(identity=ocr_probe.identity,actual_probe_confirmed=ocr_probe.verified,
                                   available_languages=ocr_probe.languages,profile=ocr_probe.probe['profile'])
    if stage in ('T05','T09A','T07','T08','T06B','T09B'):
        from translator_core.image_adapter import platform_image_adapter
        image_probe=platform_image_adapter(facts/'image-probe',image_helper)
        identity['image_adapter']=dict(identity=image_probe.identity,actual_probe_confirmed=image_probe.verified,
            available_languages=image_probe.languages,profile=image_probe.probe['profile'])
        identity['image_contracts']=dict(source='image-source-v1',facts='image-facts-v1',ocr='image-ocr-v1',body_schema=3)
    if stage in ('T09A','T07','T08','T06B','T09B'):
        identity['resource_contract']=CONTRACT
        identity['resource_binding']='resource-binding-v1'
        identity['resource_config']=str(preview_data/'resources/selection.json')
        identity['resource_reference']=str(args.resource_reference.absolute())
        identity['resource_evidence_root']=str(preview_data/'resources')
        identity['task_schemas']=[1,2,3,4,5] if stage in ('T08','T06B','T09B') else [1,2,3,4] if stage=='T07' else [1,2,3]
        if stage in ('T07','T08','T06B','T09B'): identity['audio_contract']='audio-task-v1' if stage=='T07' else 'audio-task-v2'
        if stage in ('T08','T06B','T09B'):
            identity['capture_contract'] = 'audio-recording-v1'
            identity['capture_helper'] = dict(path=str(capture.relative_to(package)),
                sha256=hashlib.sha256(capture.read_bytes()).hexdigest(),
                bundle='local.kevin.translator.'+stage.lower()+'.capture', sources=['microphone', 'system_audio'],
                system_audio='ScreenCaptureKit audio-only, microphone disabled, application output excluded')
    if stage=='T09B':
        identity.update(standalone_runtime=True,distribution='local-personal-use-runtime-closure-with-license-limits',
            python_executable=str(runtime/'Python/bin/python3.12'),worker_executable=str(runtime/'Python/bin/python3.12'),
            install_catalog=str(package/'install-catalog.json'),
            install_catalog_sha256=hashlib.sha256((package/'install-catalog.json').read_bytes()).hexdigest(),
            translation_models_root=None if local_delivery else str(args.translation_models_root.absolute()),
            package_resource_paths=package_paths,preferences_contract='translation-preferences-v1',
            managed_contract='managed-resources-v1')
        from translator_preview.paths import initialize_package_resources
        if local_delivery:
            identity.update(delivery_layout=LOCAL_DELIVERY, preview_data_root=None, preview_task_root=None,
                            preview_preferences_path=None, dictionary_config=None, resource_config=None,
                            resource_reference=None, resource_evidence_root=None,
                            install_catalog='install-catalog.json', engines='external-ollama-bundled-whisper',
                            bundled_whisper=True, product_prototype='r13.1')
        else:
            initialize_package_resources(package,identity,preview_data)
    (package / "build-identity.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in package.rglob("*.py"):
        target = (module_cache / 'python-compile' / build_id if stage in ('T08','T06B','T09B') else output / "compile-check") / path.relative_to(package).with_suffix(".pyc")
        target.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(path), cfile=str(target), doraise=True)
    if sys.platform == "darwin":
        info = {"CFBundleIdentifier": "local.kevin.translator." + stage.lower(), "CFBundleName": f"本地翻译器 {stage}预览",
            "CFBundleDisplayName": f"本地翻译器 {stage}预览", "CFBundleExecutable": "B02Preview",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": product_version,
            "CFBundleVersion": str(mac_build), "LSMinimumSystemVersion": "26.0",
            "NSDocumentsFolderUsageDescription": "此开发预览需要读取文稿目录中的项目运行环境，并保存独立的测试任务。",
            "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication"}
        if local_delivery:
            info.update(CFBundleIdentifier='local.kevin.translator.mac', CFBundleName='本地翻译器',
                        CFBundleDisplayName='本地翻译器',
                        NSDocumentsFolderUsageDescription='读取你选中的文稿以及已配置的本地语音资源。')
        if stage in ('T08','T06B','T09B'):
            info['NSMicrophoneUsageDescription'] = '在你主动开始麦克风同传后，接收选定输入设备并保存本地录音。'
            info['NSScreenCaptureUsageDescription'] = '在你主动开始电脑声音同传后，只保存系统播放的音频。'
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        executable = app / "Contents/MacOS/B02Preview"
        python_library = runtime/'Python/lib/libpython3.12.dylib' if stage=='T09B' else Path(sys.base_prefix)/'lib/libpython3.12.dylib'
        python_path = str(runtime/'Python/bin/python3.12') if stage=='T09B' else sys.executable
        flags = ['-DB01_BUNDLED_RUNTIME=1'] if stage=='T09B' else []
        subprocess.run(["clang", "-Os", "-arch", "arm64", '-DB01_PYTHON_PATH=' + json.dumps(python_path), *flags,
                        "-I", sysconfig.get_path("include"), str(source / "native/mac_launcher.c"),str(python_library),
                        "-Wl,-rpath," + ('@executable_path/../Resources/Runtime/gui/Python/lib' if stage=='T09B' else str(Path(sys.base_prefix)/'lib')),
                        "-o", str(executable)], check=True)
        if stage=='T09B':
            dependencies=run('/usr/bin/otool','-L',executable)
            for line in dependencies.splitlines()[1:]:
                dependency=line.strip().split(' (',1)[0]
                if dependency.endswith('libpython3.12.dylib') and dependency.startswith('/'):
                    subprocess.run(['/usr/bin/install_name_tool','-change',dependency,'@rpath/libpython3.12.dylib',str(executable)],check=True)
        subprocess.run(["codesign", "--sign", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    else:
        # No installer or claimed independent EXE in B01: native Python/Qt bytecode
        # build plus an explicit local interpreter launcher for desktop verification.
        # Keep cmd's bytes ASCII/CRLF. Environment variables carry Unicode paths
        # through a companion PowerShell file, outside batch source parsing.
        ps = "$ErrorActionPreference='Stop'\n$env:__PYVENV_LAUNCHER__='" + str(sys.executable).replace("'", "''") + "'\n& '" + owned_python().replace("'", "''") + "' -I -B -X utf8 (Join-Path $PSScriptRoot 'B01\\launch.py') @args\nexit $LASTEXITCODE\n"
        (output / "Launch-Preview.ps1").write_bytes(b'\xef\xbb\xbf' + ps.encode('utf-8'))
        launcher = '@echo off\r\nsetlocal\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch-Preview.ps1" %*\r\nexit /b %errorlevel%\r\n'
        (output / f"Launch-{stage}.cmd").write_bytes(launcher.encode("ascii"))
    (output / "candidate.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"build_id": build_id, "output": str(output), "source_files": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
