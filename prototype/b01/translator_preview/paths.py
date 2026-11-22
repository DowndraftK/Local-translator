"""Resolve signed package resources into one explicit writable data chain."""
from copy import deepcopy
from pathlib import Path
from translator_core.contracts import sha
from translator_core.resources import ResourceError, ResourceManager, atomic_json, json_value, no_links, safe_read
from translator_core.local_environment import (SPEECH_FOLDERS, WHISPER_ARCHIVE_ROOT, discover_speech_folder,
                                              external_speech_root, legacy_whisper_runtime,
                                              speech_files_present, speech_target, whisper_runtime_present,
                                              whisper_runtime_target)
from translator_core.resource_macos import DEFAULT_WHISPER, LEGACY_WHISPER, TRUSTED_MANIFESTS, whisper_variant


LOCAL_DELIVERY = 'mac-local-v1'
SPEECH_COMPONENTS = ('speech.runtime', 'speech.backend', 'speech.model', 'speech.assets')


def speech_origin(path, data_root):
    root = Path(path)
    if tuple(parent.name for parent in root.parents[:4]) == ('whisper', 'Runtime', 'Resources', 'Contents'):
        return 'bundled'
    return 'imported' if root.is_relative_to(data_root/'models') else 'external'


def data_directory(project, identity, explicit, task_root=None):
    local = identity.get('delivery_layout') == LOCAL_DELIVERY
    default = Path.home()/'Library/Application Support/LocalTranslator/Qt' if local else None
    selected = explicit or default or identity.get('preview_data_root')
    if not selected:
        raise ResourceError('package_data_root', '需要明确的应用数据目录。')
    root = no_links(Path(selected).absolute())
    if task_root and Path(task_root).absolute() != root/'tasks':
        raise ResourceError('package_data_root', '任务目录必须属于当前应用数据目录。')
    if root == default:
        return root
    artifacts = project/'artifacts'
    run = root.relative_to(artifacts).parts[0] if artifacts in root.parents else ''
    legacy_run = run.startswith('t09b-') and not run.startswith('t09b-prompt-')
    marker = no_links(root/'t09b-data-owner.json')
    if (legacy_run and not local) or marker.is_file():
        return root
    raise ResourceError('package_data_root', '指定的数据目录需要本应用的拥有标记。')


def managed_speech_path(managed):
    if not managed.blocked and managed.active.get('speech-runtime'):
        try:
            return managed.active_path('speech-runtime')
        except (OSError, ResourceError):
            pass
    return None


def package_whisper_runtime(package, managed, data_root, preferred):
    target = whisper_runtime_target(data_root)
    beside_app = package.parents[3]
    candidates = (preferred, target, managed_speech_path(managed) if managed else None,
                  beside_app/WHISPER_ARCHIVE_ROOT, legacy_whisper_runtime())
    for root in dict.fromkeys(candidates):
        if root and external_speech_root(root) and whisper_runtime_present({'root': str(root)}):
            return root
    return target


def package_resource_defaults(package, identity, data_root, managed=None, selection=None):
    template = package/'resource-reference-template.json'
    if not template.is_file():
        return None
    value = json_value(safe_read(template))
    replacements = identity.get('package_resource_paths', {})
    local = identity.get('delivery_layout') == LOCAL_DELIVERY
    runtime = None
    external_presets = identity.get('external_speech_presets')
    if local and not (identity.get('bundled_whisper') or external_presets):
        preferred = next((Path(spec['root']) for spec in (selection or {}).get('resources', [])
                          if spec['resource_id'] == 'speech.runtime'), None)
        runtime = package_whisper_runtime(package, managed, data_root, preferred)
    for spec in value['resources']:
        name = spec['resource_id']
        paths = replacements.get(name)
        if paths and 'root' in paths:
            root = Path(paths['root'])
            spec['root'] = str(root if root.is_absolute() else (package/root).resolve())
        if paths:
            spec['manifest']['path'] = str((package/paths['manifest']).resolve())
        if runtime and name in ('speech.runtime', 'speech.backend'):
            spec['root'] = str(runtime)
            if name == 'speech.runtime':
                spec['manifest']['path'] = str(runtime/'component.json')
        if external_presets and name in SPEECH_FOLDERS:
            spec['root'] = str(speech_target(data_root, name, spec))
        elif local and not identity.get('bundled_whisper') and name in SPEECH_FOLDERS:
            spec['root'] = str(discover_speech_folder(data_root, spec, package.parents[3]))
    return value


def initialize_package_resources(package, identity, data_root, *, managed=None, manager=None):
    local = identity.get('delivery_layout') == LOCAL_DELIVERY
    replacements = identity.get('package_resource_paths', {})
    if not (package/'resource-reference-template.json').is_file():
        return
    manager = manager or ResourceManager(data_root)
    if manager.config_blocked or manager.dirty:
        return
    value = package_resource_defaults(package, identity, data_root, managed, manager.saved_selection)
    if manager.selection:
        current = deepcopy(manager.selection['resources'])
        defaults = {spec['resource_id']: spec for spec in value['resources']}
        preset_delivery = identity.get('external_speech_presets')
        if identity.get('bundled_whisper') or preset_delivery:
            changes = upgraded_speech_resources(package, data_root, value, manager.saved_selection,
                                                external_only=bool(preset_delivery))
            current = [changes.pop(spec['resource_id'], spec) for spec in current]
            current.extend(changes.values())
        for spec in current:
            replacement = defaults.get(spec['resource_id'])
            if (identity.get('bundled_whisper') or preset_delivery) and spec['resource_id'] in SPEECH_COMPONENTS:
                continue
            if local and replacement and spec['resource_id'] == 'speech.bridge':
                spec.update(replacement)
                continue
            if local and replacement and spec['resource_id'] == 'speech.model':
                variant = whisper_variant(spec['manifest']['sha256'])
                if variant:
                    spec.update(package_speech_variants(package,data_root,value,manager.saved_selection)[variant])
                continue
            if (replacement and spec['resource_id'] in replacements and
                    spec['manifest']['sha256']==replacement['manifest']['sha256'] and
                    (spec['root'],spec['manifest']['path']) != (replacement['root'],replacement['manifest']['path'])):
                if spec['resource_id'] in ('gui.runtime', 'speech.bridge', 'mac.frameworks') or (
                    local and spec['resource_id'] in ('speech.runtime', 'speech.backend')
                ):
                    spec['root'] = replacement['root']
                if local and spec['resource_id'] in SPEECH_FOLDERS and (
                    not external_speech_root(spec['root']) or not speech_files_present(spec)
                ):
                    spec['root'] = replacement['root']
                spec['manifest']['path'] = replacement['manifest']['path']
        if current == manager.selection['resources']:
            return
        value = dict(value,resources=current)
    if local:
        path = manager.root/f"package-reference-{identity['build_id']}-{manager.revision + 1}.json"
    else:
        path = manager.root/('package-reference-'+identity['build_id']+'.json')
    atomic_json(path, value)
    manager.select(path)
    manager.save()


def upgraded_speech_resources(package, data_root, defaults, selection, *, external_only=False):
    """Relocate bundled files and retain validated external choices across upgrades."""
    saved = {spec['resource_id']: spec for spec in selection['resources']}
    choices = {spec['resource_id']: dict(spec) for spec in defaults['resources']
               if spec['resource_id'] in SPEECH_COMPONENTS}
    runtime = saved.get('speech.runtime')
    if (not external_only and runtime and speech_origin(runtime['root'], data_root)!='bundled'
            and external_speech_root(runtime['root']) and whisper_runtime_present(runtime)):
        root = runtime['root']
        choices['speech.runtime'].update(root=root)
        choices['speech.runtime']['manifest'] = dict(choices['speech.runtime']['manifest'],
                                                      path=str(Path(root)/'component.json'))
        choices['speech.backend'].update(root=root)
    assets = saved.get('speech.assets')
    if assets and not in_app(assets['root']) and external_speech_root(assets['root']):
        candidate = dict(choices['speech.assets'], root=assets['root'])
        if speech_files_present(candidate):
            choices['speech.assets'] = candidate
    old_model = saved.get('speech.model')
    if old_model and not in_app(old_model['root']):
        variant = whisper_variant(old_model['manifest']['sha256'])
        if variant:
            model = package_speech_variants(package, data_root, defaults, selection,
                                            external_only=external_only)[variant]
            if speech_files_present(model):
                choices['speech.model'] = model
    return choices


def in_app(path):
    return any(part.lower().endswith('.app') for part in Path(path).parts)


def package_speech_variants(package, data_root, defaults, selection=None, *, external_only=False):
    default = next(row for row in defaults['resources'] if row['resource_id']=='speech.model')
    manifest = package/'shared/translator_core/whisper-large-v3-turbo.model.json'
    raw = safe_read(manifest, 4096)
    digest = sha(raw)
    if digest != TRUSTED_MANIFESTS['speech.model']:
        raise ResourceError('resource_corrupt','内置多语模型校验清单已改变。')
    multi = dict(default, role='Whisper large-v3-turbo 多语模型', version=json_value(raw)['revision'],
                 origin='https://huggingface.co/mlx-community/whisper-large-v3-turbo',
                 root=str(data_root/'models/whisper-large-v3-turbo'),
                 manifest=dict(path=str(manifest), bytes=len(raw), sha256=digest, format='model-v0'))
    selected = next((row for row in (selection or {}).get('resources', [])
                     if row['resource_id']=='speech.model'), None)
    result = {}
    for variant, spec in ((DEFAULT_WHISPER, default), (LEGACY_WHISPER, multi)):
        chosen_root = None
        if selected and whisper_variant(selected['manifest']['sha256'])==variant:
            path = selected['root']
            current = speech_origin(path,data_root)!='bundled' or path==spec['root']
            if (current and (not external_only or not in_app(path)) and external_speech_root(path)
                    and speech_files_present(dict(spec,root=path))):
                chosen_root = Path(path)
        if not chosen_root:
            local = speech_target(data_root, 'speech.model', spec)
            imports = sorted(local.parent.glob(local.name+'-import-*'))
            chosen_root = next((folder for folder in (local,*imports) if speech_files_present(
                dict(spec,root=str(folder)))), None)
        if not chosen_root and not external_only:
            if speech_origin(spec['root'],data_root)=='bundled' and speech_files_present(spec):
                chosen_root = Path(spec['root'])
        if not chosen_root:
            chosen_root = local if external_only else discover_speech_folder(data_root,spec,package.parents[3])
        result[variant] = dict(spec,root=str(chosen_root))
    return result


def select_speech_root(package, identity, data_root, managed, manager, resource_id, root, *, model_spec=None):
    value = package_resource_defaults(package, identity, data_root, managed, manager.saved_selection)
    defaults = {row['resource_id']: row for row in value['resources']}
    root = Path(root)
    spec = dict(model_spec or defaults[resource_id], root=str(no_links(root)))
    check = whisper_runtime_present if resource_id == 'speech.runtime' else speech_files_present
    if not external_speech_root(root) or not check(spec):
        raise ResourceError('resource_missing', '文件夹缺少与此 App 兼容的语音文件。')
    changes = {resource_id: spec}
    if resource_id == 'speech.runtime':
        spec['manifest'] = dict(spec['manifest'], path=str(root/'component.json'))
        changes['speech.backend'] = dict(defaults['speech.backend'], root=str(root))
    if resource_id == 'speech.model':
        changes['speech.bridge'] = defaults['speech.bridge']
    save_speech_selection(identity, manager, value, changes)


def restore_bundled_speech(package, identity, manager):
    value = package_resource_defaults(package,identity,manager.root.parent)
    choices = {spec['resource_id']: spec for spec in value['resources']
               if spec['resource_id'] in (*SPEECH_COMPONENTS,'speech.bridge')}
    if (not identity.get('bundled_whisper') or not whisper_runtime_present(choices.get('speech.runtime'))
            or not all(speech_files_present(choices.get(name)) for name in SPEECH_FOLDERS)):
        raise ResourceError('resource_missing','内置语音文件缺失或损坏，请使用完整的 App 安装包。')
    save_speech_selection(identity, manager, value, choices)


def save_speech_selection(identity, manager, value, changes):
    resources = manager.selection['resources'] if manager.selection else value['resources']
    updated = [changes.pop(row['resource_id'], row) for row in resources]
    updated.extend(changes.values())
    if resources != updated:
        path = manager.root/f"package-reference-{identity['build_id']}-{manager.revision + 1}.json"
        atomic_json(path, dict(value, resources=updated))
        manager.select(path)
        manager.save()
