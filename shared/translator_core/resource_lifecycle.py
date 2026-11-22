"""Owned versions, bounded copy/verification and recoverable activation records."""
import hashlib
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from . import CoreError
from .resources import (CHUNK, CHECK_SECONDS, FILE_COUNT, SET_LIMIT, ResourceError, atomic_json,
                        content_identity, encoded, json_value, manifest_entries, no_links,
                        open_readonly, relative_name, safe_read, stat_token, stop_check, token,
                        validate_spec, verify_resource, bounded_string)

CONTRACT = 'managed-resources-v1'
CATALOG = 'resource-install-catalog-v1'
ACTIVATION = 'resource-activation-v1'
TRANSACTION = 'resource-transaction-v1'
OWNER = 'local-translator-managed-version-v1'
ACTIONS = ('prepare', 'repair', 'rollback', 'uninstall', 'recover')
TERMINAL = ('completed', 'cancelled', 'failed', 'recovered')


def component_id(value):
    if not isinstance(value, str) or not re.fullmatch('[a-z][a-z0-9-]{0,47}', value):
        raise ResourceError('resource_incompatible', '资源组件身份无效。')
    return value


def validate_catalog(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'components'} or
            value['version'] != CATALOG or not isinstance(value['components'], list) or
            not 1 <= len(value['components']) <= 8):
        raise ResourceError('resource_incompatible', '安装清单字段或版本不受支持。')
    seen = set()
    for item in value['components']:
        if (not isinstance(item, dict) or set(item) != {'id', 'title', 'resource', 'dependencies',
                'license_materials', 'distribution_status'} or component_id(item['id']) in seen or
                not isinstance(item['title'], str) or len(item['title'].encode()) > 256 or
                not isinstance(item['dependencies'], list) or len(item['dependencies']) > 8 or
                not isinstance(item['license_materials'], list) or len(item['license_materials']) > 32):
            raise ResourceError('resource_incompatible', '安装组件字段无效。')
        validate_spec(item['resource'])
        bounded_string(item['distribution_status'], 1024)
        for material in item['license_materials']:
            bounded_string(material, 4096)
        for dependency in item['dependencies']:
            component_id(dependency)
        if len(set(item['dependencies'])) != len(item['dependencies']) or item['id'] in item['dependencies']:
            raise ResourceError('resource_incompatible', '安装组件含重复或自身依赖。')
        seen.add(item['id'])
    if any(d not in seen for c in value['components'] for d in c['dependencies']):
        raise ResourceError('resource_incompatible', '安装组件依赖缺少身份。')
    resolved = set()
    for _ in value['components']:
        resolved.update(c['id'] for c in value['components'] if set(c['dependencies']) <= resolved)
    if resolved != seen:
        raise ResourceError('resource_incompatible', '安装组件依赖形成循环。')
    return value


def copy_verified_file(source, target, expected, cancel, deadline, progress):
    """Copy a regular file into private staging and verify the actual bytes read."""
    stop_check(cancel, deadline)
    deadline = min(deadline, time.monotonic() + 120)
    target = no_links(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(open_readonly(source), 'rb') as stream:
        before = stat_token(os.fstat(stream.fileno()))
        if not stat.S_ISREG(before[2]) or before[3] != expected['size']:
            raise ResourceError('resource_corrupt', '来源大小或类型不匹配。')
        hashed = hashlib.sha256()
        count = 0
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            while block := stream.read(CHUNK):
                stop_check(cancel, deadline)
                count += len(block)
                if count > expected['size']:
                    raise ResourceError('resource_limit', '资源复制超过已冻结大小。')
                output.write(block)
                hashed.update(block)
                progress(count)
            output.flush()
            os.fsync(output.fileno())
        if (count != expected['size'] or hashed.hexdigest() != expected['sha256'] or
                before != stat_token(os.fstat(stream.fileno())) or before != token(source)):
            raise ResourceError('resource_changed', '复制期间来源改变或校验不符。')
        os.chmod(target, stat.S_IMODE(before[2]) & 0o755)


def copy_manifest_resource(source, payload, cancel, deadline, progress):
    """Stage a frozen component using its declared files and internal links."""
    entries, manifest_token, total = manifest_entries(source)
    root = no_links(source['root'])
    manifest_path = Path(source['manifest']['path'])
    all_entries = dict(entries)
    if manifest_path.is_relative_to(root):
        all_entries[str(manifest_path.relative_to(root))] = dict(
            size=source['manifest']['bytes'], sha256=source['manifest']['sha256'])
        total += source['manifest']['bytes']
    if total > SET_LIMIT:
        raise ResourceError('resource_limit', '资源复制超过已冻结大小。')
    if shutil.disk_usage(payload.parent).free < total + 64 * 1024**2:
        raise ResourceError('insufficient_space', '准备空间不足，请释放磁盘空间后重试。')
    payload.mkdir(mode=0o700)
    copied = 0
    for index, (name, expected) in enumerate(all_entries.items()):
        stop_check(cancel, deadline)
        path = root/relative_name(name)
        destination = payload/name
        if 'link' in expected:
            no_links(destination.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        copy_verified_file(path, destination, expected, cancel, deadline,
                           lambda count: progress(index, len(all_entries), copied+count, total))
        copied += expected['size']
    for name, expected in entries.items():
        if 'link' not in expected:
            continue
        src = root/name
        no_links(src.parent)
        if not src.is_symlink() or os.readlink(src) != expected['link']:
            raise ResourceError('unsafe_path', '资源链接与受信任清单不配。')
        dst = payload/name
        no_links(dst.parent)
        os.symlink(expected['link'], dst)
    # Some dylib aliases depend on a second declared link.
    for name, expected in entries.items():
        if 'link' not in expected:
            continue
        stop_check(cancel, deadline)
        try:
            final = (payload/name).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ResourceError('unsafe_path', '资源内部链接无法安全解析。') from exc
        if not final.is_relative_to(payload) or final == payload:
            raise ResourceError('unsafe_path', '资源内部链接没有受保护的同根最终文件。')
        target = str(final.relative_to(payload))
        protected_directory = final.is_dir() and any(
            key.startswith(target+'/') and 'sha256' in row for key, row in entries.items())
        if not protected_directory and (target not in entries or 'sha256' not in entries[target]):
            raise ResourceError('unsafe_path', '资源内部链接没有受保护的同根最终文件。')
    if token(manifest_path) != manifest_token:
        raise ResourceError('resource_changed', '复制期间来源清单改变。')


class ManagedResources:
    def __init__(self, data_root, catalog_path=None, catalog_sha256=None, *, package_sources=None):
        self.root = no_links(Path(data_root).absolute()) / 'managed'
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ('versions', 'staging', 'transactions'):
            no_links(self.root/name).mkdir(exist_ok=True, mode=0o700)
        self.path = self.root/'activation.json'
        self.catalog = {'version': CATALOG, 'components': []}
        self.catalog_id = None
        self.warning = None
        self.active = {}
        self.records = {}
        self.revision = self.generation = 0
        self.baseline = self.baseline_token = None
        self.blocked = False
        self.current = None
        self.state_lock = threading.RLock()
        if catalog_path:
            raw = safe_read(Path(catalog_path).absolute())
            if catalog_sha256 and hashlib.sha256(raw).hexdigest() != catalog_sha256:
                raise ResourceError('resource_corrupt', '安装清单与构建冻结身份不符。')
            value = json_value(raw)
            if package_sources is not None:
                if not catalog_sha256:
                    raise ResourceError('resource_incompatible', '包内安装清单缺少冻结身份。')
                value = self._resolve_package_catalog(value, no_links(package_sources))
            self.catalog = validate_catalog(value)
            self.catalog_id = hashlib.sha256(raw).hexdigest()
        self._load()

    @staticmethod
    def _resolve_package_catalog(value, base):
        try:
            for item in value['components']:
                resource = item['resource']
                resource['root'] = str(base/relative_name(resource['root']))
                manifest = resource['manifest']
                manifest['path'] = str(base/relative_name(manifest['path']))
                item['license_materials'] = [str(base/relative_name(path)) for path in item['license_materials']]
        except (TypeError, KeyError) as exc:
            raise ResourceError('resource_incompatible', '包内安装清单字段无效。') from exc
        return value

    def _disk(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None, None
        raw = safe_read(self.path, 512*1024)
        return raw, token(self.path)

    def _load(self):
        try:
            raw, disk_token = self._disk()
            if raw is None:
                self.active, self.records, self.revision = {}, {}, 0
            if raw is not None:
                value = json_value(raw)
                if (not isinstance(value, dict) or set(value) != {'version', 'revision', 'active', 'records'} or
                        value['version'] != ACTIVATION or type(value['revision']) is not int or
                        not 0 < value['revision'] < 2**53 or not isinstance(value['active'], dict) or
                        not isinstance(value['records'], dict) or len(value['records']) > 64):
                    raise ResourceError('resource_incompatible', '资源激活记录损坏或版本过新。')
                for key, record in value['records'].items():
                    self._validate_record(key, record)
                for key, version in value['active'].items():
                    component_id(key)
                    if version not in value['records'] or value['records'][version]['component'] != key:
                        raise ResourceError('resource_incompatible', '激活版本身份不配。')
                self.active, self.records, self.revision = value['active'], value['records'], value['revision']
            self.baseline, self.baseline_token = raw, disk_token
            self.blocked = False
            self.warning = None
        except (OSError, CoreError, KeyError, TypeError) as exc:
            self.blocked = True
            self.warning = str(exc)

    @staticmethod
    def _validate_record(key, record):
        if (not isinstance(key, str) or not re.fullmatch('[0-9a-f]{32}', key) or not isinstance(record, dict) or
                set(record) != {'component', 'instance', 'content_identity', 'catalog_id', 'relative', 'created_at'} or
                record['instance'] != key or record['relative'] != 'versions/'+record['component']+'/'+key or
                not re.fullmatch('[0-9a-f]{64}', record['content_identity']) or
                not re.fullmatch('[0-9a-f]{64}', record['catalog_id']) or
                type(record['created_at']) not in (int, float)):
            raise ResourceError('resource_incompatible', '资源拥有记录无效。')
        component_id(record['component'])

    def _save(self):
        try:
            matches = self._disk() == (self.baseline, self.baseline_token)
        except (OSError, CoreError) as exc:
            self.blocked = True
            self.warning = '资源激活记录读取未确认，请使用恢复入口核对。'
            raise ResourceError('activation_unconfirmed', self.warning) from exc
        if self.blocked or not matches:
            self.blocked = True
            self.warning = '资源激活记录已改变或读取受阻，请使用恢复入口重新核对。'
            raise ResourceError('activation_changed', '资源激活记录已改变或读取受阻，请重新打开核对。')
        value = dict(version=ACTIVATION, revision=self.revision+1, active=self.active, records=self.records)
        try:
            atomic_json(self.path, value)
            actual, actual_token = self._disk()
            if actual != encoded(value):
                raise ResourceError('activation_unconfirmed', '资源激活写入尚未确认。')
        except (OSError, CoreError) as exc:
            self.blocked = True
            self.warning = '激活记录写入尚待核对，请重新打开恢复。'+str(exc)
            raise ResourceError('activation_unconfirmed', self.warning) from exc
        self.baseline, self.baseline_token = actual, actual_token
        self.revision += 1

    def component(self, name):
        component_id(name)
        item = next((c for c in self.catalog['components'] if c['id'] == name), None)
        if item is None:
            raise ResourceError('resource_missing', '本候选没有该组件的受信任来源。')
        return item

    def folder(self, record):
        self._validate_record(record['instance'], record)
        folder = no_links(self.root/record['relative'])
        marker = json_value(safe_read(folder/'owner.json', 16384))
        if marker != dict(version=OWNER, record=record):
            raise ResourceError('ownership_mismatch', '目标目录缺少匹配的应用拥有记录，已保留。')
        return folder

    def active_path(self, name):
        if self.blocked:
            raise ResourceError('activation_unconfirmed', self.warning or '激活记录待核对。')
        instance = self.active.get(name)
        if not instance:
            raise ResourceError('resource_missing', '尚未准备'+self.component(name)['title']+'。')
        return self.folder(self.records[instance])/'payload'

    def spec_at(self, item, payload):
        spec = deepcopy(item['resource'])
        original_root = Path(spec['root'])
        manifest = Path(spec['manifest']['path'])
        spec['root'] = str(payload)
        if manifest.is_relative_to(original_root):
            spec['manifest']['path'] = str(payload/manifest.relative_to(original_root))
        return spec

    def verify(self, item, payload, cancel, deadline, progress):
        spec = self.spec_at(item, payload)
        result = verify_resource(spec, cancel, deadline, progress)
        entries, _, _ = manifest_entries(spec)
        allowed = set(entries)
        manifest = Path(spec['manifest']['path'])
        if manifest.is_relative_to(payload):
            allowed.add(str(manifest.relative_to(payload)))
        observed = set()
        for base, dirs, names in os.walk(payload, followlinks=False):
            stop_check(cancel, deadline)
            for name in tuple(dirs):
                if (Path(base)/name).is_symlink():
                    dirs.remove(name)
                    names.append(name)
            for name in names:
                observed.add(str((Path(base)/name).relative_to(payload)))
                if len(observed) > FILE_COUNT+1:
                    raise ResourceError('resource_limit', '受控版本实际文件数量超限。')
        if observed != allowed:
            raise ResourceError('resource_corrupt', '受控版本含未登记文件或缺少文件。')
        return result

    @contextmanager
    def locked(self):
        import fcntl
        fd = os.open(no_links(self.root/'.operation.lock'), os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ResourceError('busy', '另一实例正在更改资源。') from exc
            yield
        finally:
            os.close(fd)

    def owner(self, session, request, operation, preferences_revision, selection_revision, resource_set_id):
        self.generation += 1
        return dict(worker_session=session, request_id=request, operation_id=operation,
                    configuration_revision=preferences_revision, selection_revision=selection_revision,
                    resource_set_id=resource_set_id, generation=self.generation)

    def _transaction(self, value, stage, **fields):
        with self.state_lock:
            value.update(stage=stage, updated_at=time.time(), **fields)
            atomic_json(self.root/'transactions'/(value['id']+'.json'), value)
            self.current = deepcopy(value)

    def copy(self, item, payload, cancel, deadline, report, transaction):
        def copied(index, files, count, _):
            transaction['copy_bytes'] = count
            report('copying', index, files, count)

        copy_manifest_resource(item['resource'], payload, cancel, deadline, copied)

    def _remove_owned(self, folder, cancel, deadline):
        no_links(folder)
        count = [0]
        def remove_contents(descriptor):
            for name in os.listdir(descriptor):
                stop_check(cancel, deadline)
                relative_name(name)
                before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    child = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        if stat_token(before) != stat_token(os.fstat(child)):
                            raise ResourceError('resource_changed', '删除期间目录发生变化。')
                        remove_contents(child)
                    finally:
                        os.close(child)
                    os.rmdir(name, dir_fd=descriptor)
                elif stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                    os.unlink(name, dir_fd=descriptor)
                    count[0] += 1
                    if count[0] > FILE_COUNT+32:
                        raise ResourceError('resource_limit', '删除文件数超过受控版本上限。')
                else:
                    raise ResourceError('unsafe_path', '受控目录含异常特殊文件，保留待核对。')
        descriptor = open_readonly(folder)
        try:
            remove_contents(descriptor)
        finally:
            os.close(descriptor)
        folder.rmdir()

    def operate(self, action, name, owner, cancel, progress, *, instance=None, confirmed=False,
                protected_roots=(), fault=None, before_change=None):
        if action not in ACTIONS:
            raise ResourceError('resource_incompatible', '资源动作不受支持。')
        started = time.monotonic()
        deadline = started + CHECK_SECONDS
        transaction = dict(version=TRANSACTION, id=owner['operation_id'].replace('-', ''), owner=owner,
                           action=action, component=name, instance=instance, stage='started',
                           started_at=time.time(), copy_bytes=0, hash_bytes=0, error=None, confirmed=confirmed)
        def report(stage, completed, total, count):
            progress(stage, completed, total, count)
        with self.locked():
            if action == 'recover':
                with self.state_lock:
                    self._load()
            if self.blocked:
                raise ResourceError('activation_unconfirmed', self.warning or '资源激活记录待核对。')
            if self._disk() != (self.baseline, self.baseline_token):
                raise ResourceError('activation_changed', '资源激活记录被外部修改，请重新打开。')
            self._transaction(transaction, 'started')
            try:
                if action == 'recover':
                    self._recover(transaction, cancel, deadline, before_change, protected_roots)
                    self._transaction(transaction, 'completed')
                else:
                    item = self.component(name)
                    if action in ('prepare', 'repair', 'rollback'):
                        missing = [d for d in item['dependencies'] if d not in self.active]
                        if missing:
                            raise ResourceError('dependency_missing', '请先准备依赖组件。'+'、'.join(missing))
                    if action in ('prepare', 'repair'):
                        self._prepare(item, transaction, cancel, deadline, report, action == 'repair', fault,
                                      before_change, protected_roots)
                    elif action == 'rollback':
                        self._rollback(item, transaction, instance, cancel, deadline, report, protected_roots, before_change)
                    else:
                        self._uninstall(item, transaction, instance, confirmed, cancel, deadline, protected_roots, before_change)
            except (OSError, CoreError, ValueError) as exc:
                code = getattr(exc, 'code', 'resource_io')
                self._transaction(transaction, 'cancelled' if code == 'check_cancelled' else 'failed',
                                  error=dict(code=code, message=str(exc)), elapsed_seconds=time.monotonic()-started)
            else:
                self._transaction(transaction, 'completed', elapsed_seconds=time.monotonic()-started)
        return dict(owner=owner, description=self.describe(), transaction=deepcopy(transaction))

    def _prepare(self, item, transaction, cancel, deadline, report, repair, fault, before_change, protected_roots):
        previous = self.active.get(item['id'])
        if previous and not repair:
            record = self.records[previous]
            if record['content_identity'] != content_identity(item['resource']):
                raise ResourceError('resource_incompatible', '已激活内容与清单不配，请使用修复入口。')
            self.verify(item, self.folder(record)/'payload', cancel, deadline, report)
            transaction.update(instance=previous, reused=True)
            return
        if previous:
            self._protect(self.folder(self.records[previous])/'payload', protected_roots)
        if len(self.records) >= 64:
            raise ResourceError('resource_limit', '已保留版本达到上限，请先卸载不再使用的版本。')
        operation = transaction['id']
        stage = self.root/'staging'/operation
        if stage.exists():
            raise ResourceError('transaction_exists', '同一事务已存在，请先恢复或刷新。')
        stage.mkdir(mode=0o700)
        marker = dict(version=OWNER, operation=operation, component=item['id'], catalog_id=self.catalog_id)
        atomic_json(stage/'owner.json', marker)
        payload = stage/'payload'
        self._transaction(transaction, 'copying')
        self.copy(item, payload, cancel, deadline, report, transaction)
        if repair and item['id'] == 'dictionary':
            from .dictionary.resources import build_index
            config = json_value(safe_read(payload/'resources.json', 32768))
            self._transaction(transaction, 'rebuilding')
            for spec in config['resources']:
                path = no_links(payload/spec['index_path'])
                path.unlink()
                build_index(payload/spec['source_path'], path, spec['direction'],
                            source_sha256=spec['source_sha256'], source_version=spec['source_version'],
                            record_count=spec['records'], cancel=cancel, deadline=deadline)
        self._transaction(transaction, 'verifying')
        def hashing(_, completed, total, count):
            transaction['hash_bytes'] = count
            report('verifying', completed, total, count)
        self.verify(item, payload, cancel, deadline, hashing)
        stop_check(cancel, deadline)
        if fault == 'before_activation':
            raise ResourceError('injected_failure', '受控夹具在激活前中断。')
        if before_change:
            before_change(item['id'])
        record = dict(component=item['id'], instance=operation, catalog_id=self.catalog_id,
                      content_identity=content_identity(item['resource']),
                      relative='versions/'+item['id']+'/'+operation, created_at=time.time())
        target = no_links(self.root/record['relative'])
        target.parent.mkdir(exist_ok=True, mode=0o700)
        atomic_json(stage/'owner.json', dict(version=OWNER, record=record))
        self._transaction(transaction, 'prepared', instance=operation)
        os.rename(stage, target)
        with self.state_lock:
            self.records[operation] = record
            self._transaction(transaction, 'activating')
            self.active[item['id']] = operation
            self._save()
        if fault == 'after_activation':
            raise ResourceError('injected_failure', '受控夹具在激活已确认后中断。')

    def _protect(self, payload, protected_roots):
        if any(Path(p) == payload or Path(p).is_relative_to(payload) for p in protected_roots):
            raise ResourceError('resource_in_use', '当前任务持有这个资源版本，请先切换到其他任务再操作。')

    def _rollback(self, item, transaction, instance, cancel, deadline, report, protected_roots, before_change):
        candidates = [key for key, row in self.records.items() if row['component'] == item['id']
                      and key != self.active.get(item['id'])]
        if instance is None:
            instance = candidates[-1] if candidates else None
        if instance not in candidates:
            raise ResourceError('rollback_missing', '没有可回退的已拥有版本。')
        previous = self.active.get(item['id'])
        if previous:
            self._protect(self.folder(self.records[previous])/'payload', protected_roots)
        record = self.records[instance]
        if record['content_identity'] != content_identity(item['resource']):
            raise ResourceError('resource_incompatible', '该版本与当前来源清单不兼容。')
        self._transaction(transaction, 'verifying', instance=instance)
        self.verify(item, self.folder(record)/'payload', cancel, deadline,
                    lambda _, a, b, count: report('verifying', a, b, count))
        stop_check(cancel, deadline)
        if before_change:
            before_change(item['id'])
        with self.state_lock:
            self.active[item['id']] = instance
            self._transaction(transaction, 'activating')
            self._save()

    def _uninstall(self, item, transaction, instance, confirmed, cancel, deadline, protected_roots, before_change):
        if confirmed is not True:
            raise ResourceError('confirmation_required', '请明确确认资源版本与受影响功能后卸载。')
        instance = instance or self.active.get(item['id'])
        record = self.records.get(instance)
        if not record or record['component'] != item['id']:
            raise ResourceError('resource_missing', '所选受控版本已经不存在，请刷新。')
        folder = self.folder(record)
        self._protect(folder/'payload', protected_roots)
        dependents = [c['title'] for c in self.catalog['components']
                      if item['id'] in c['dependencies'] and c['id'] in self.active]
        if dependents and self.active.get(item['id']) == instance:
            raise ResourceError('resource_in_use', '仍有已激活依赖使用此组件。'+'、'.join(dependents))
        self._transaction(transaction, 'deleting', instance=instance, record=record)
        if before_change:
            before_change(item['id'])
        with self.state_lock:
            if self.active.get(item['id']) == instance:
                self.active.pop(item['id'])
                self._save()
        # Preserve the ownership marker until every payload entry is gone.
        payload = no_links(folder/'payload')
        if payload.exists():
            self._remove_owned(payload, cancel, deadline)
        with self.state_lock:
            self.records.pop(instance)
            self._save()
        (folder/'owner.json').unlink()
        folder.rmdir()

    def _read_transaction(self, path):
        value = json_value(safe_read(path, 32768))
        if (not isinstance(value, dict) or value.get('version') != TRANSACTION or
                not isinstance(value.get('id'), str) or not re.fullmatch('[0-9a-f]{32}', value['id']) or
                path.stem != value['id'] or value.get('action') not in ACTIONS or
                value.get('stage') not in (*TERMINAL, 'started', 'copying', 'rebuilding', 'verifying',
                                          'prepared', 'activating', 'deleting')):
            raise ResourceError('resource_incompatible', '事务记录身份、阶段或版本不受支持。')
        return value

    def _recover(self, transaction, cancel, deadline, before_change, protected_roots):
        for stage in sorted((self.root/'staging').iterdir()):
            stop_check(cancel, deadline)
            if not re.fullmatch('[0-9a-f]{32}', stage.name):
                raise ResourceError('ownership_mismatch', '中断目录身份无效，已保留。')
            no_links(stage)
            marker = json_value(safe_read(stage/'owner.json', 16384))
            staged_marker = (marker.get('operation') == stage.name and marker.get('catalog_id') == self.catalog_id)
            prepared_record = marker.get('record')
            if prepared_record:
                self._validate_record(stage.name, prepared_record)
                staged_marker = prepared_record['catalog_id'] == self.catalog_id
            if marker.get('version') != OWNER or not staged_marker:
                raise ResourceError('ownership_mismatch', '中断目录的拥有记录不匹配。')
            self._remove_owned(stage, cancel, deadline)
        for path in sorted((self.root/'transactions').glob('*.json')):
            stop_check(cancel, deadline)
            value = self._read_transaction(path)
            record = value.get('record')
            if (value.get('action') == 'uninstall' and value.get('confirmed') is True and
                    record and record.get('instance') in self.records):
                self._uninstall(self.component(record['component']), transaction, record['instance'],
                                True, cancel, deadline, protected_roots, before_change)
            elif value.get('action') == 'uninstall' and value.get('confirmed') is True and record:
                self._validate_record(record.get('instance'), record)
                folder = no_links(self.root/record['relative'])
                if folder.exists():
                    marker = json_value(safe_read(folder/'owner.json', 16384))
                    if marker != dict(version=OWNER, record=record) or (folder/'payload').exists():
                        raise ResourceError('ownership_mismatch', '已移除版本的尾部清理待核对。')
                    (folder/'owner.json').unlink()
                    folder.rmdir()
            if value.get('stage') not in TERMINAL:
                if value['id'] != transaction['id']:
                    self._transaction(value, 'recovered')
        # A crash between version publication and activation leaves an owned,
        # inactive version. Recover its record without guessing an active value.
        adopted = False
        for component in self.catalog['components']:
            parent = self.root/'versions'/component['id']
            if not parent.is_dir():
                continue
            for folder in parent.iterdir():
                if folder.name in self.records:
                    continue
                marker = json_value(safe_read(no_links(folder)/'owner.json', 16384))
                record = marker.get('record')
                self._validate_record(folder.name, record)
                if marker.get('version') != OWNER or record['catalog_id'] != self.catalog_id:
                    raise ResourceError('ownership_mismatch', '未激活版本的拥有记录不匹配。')
                self.verify(component, folder/'payload', cancel, deadline, lambda *_:None)
                with self.state_lock:
                    self.records[folder.name] = record
                adopted = True
        if adopted:
            with self.state_lock:
                self._save()

    def describe(self):
        with self.state_lock:
            records = deepcopy(self.records)
            active = dict(self.active)
            revision, generation = self.revision, self.generation
            current = deepcopy(self.current)
        rows = []
        for item in self.catalog['components']:
            versions = []
            for instance, record in records.items():
                if record['component'] != item['id']:
                    continue
                try:
                    payload = self.folder(record)/'payload'
                    state = 'present' if payload.is_dir() else 'missing'
                except (OSError, CoreError):
                    payload = self.root/record['relative']/'payload'
                    state = 'ownership_unconfirmed'
                versions.append(dict(instance=instance, path=str(payload), state=state,
                                     active=active.get(item['id']) == instance))
            rows.append(dict(id=item['id'], title=item['title'], version=item['resource']['version'],
                             active=active.get(item['id']), versions=versions,
                             ownership='application_managed', origin=item['resource']['origin'],
                             license=item['resource']['license'], license_materials=item['license_materials'],
                             distribution_status=item['distribution_status'], dependencies=item['dependencies']))
        pending = []
        paths = sorted((self.root/'transactions').glob('*.json'))
        if len(paths) > 128:
            raise ResourceError('resource_limit', '事务记录达到本版本上限。')
        for path in paths:
            try:
                with self.state_lock:
                    value = self._read_transaction(path)
            except (OSError, CoreError) as exc:
                self.blocked = True
                self.warning = '事务记录待核对，原文件已保留。'+str(exc)
                continue
            if value.get('stage') not in TERMINAL or (self.root/'staging'/str(value.get('id'))).exists():
                pending.append(dict(id=value.get('id'), stage=value.get('stage'), component=value.get('component')))
        return dict(version=CONTRACT, revision=revision, generation=generation,
                    catalog_id=self.catalog_id, root=str(self.root), blocked=self.blocked,
                    warning=self.warning, components=rows, pending=pending, current=current)


def validate_description(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'revision', 'generation', 'catalog_id',
            'root', 'blocked', 'warning', 'components', 'pending', 'current'} or value['version'] != CONTRACT or
            any(type(value[k]) is not int or not 0 <= value[k] < 2**53 for k in ('revision', 'generation')) or
            type(value['blocked']) is not bool or not isinstance(value['components'], list) or
            len(value['components']) > 8 or not isinstance(value['pending'], list) or len(value['pending']) > 128 or
            len(encoded(value)) > 48000):
        raise ResourceError('resource_incompatible', '资源生命周期响应字段或版本无效。')
    for item in value['components']:
        if not isinstance(item, dict) or set(item) != {'id', 'title', 'version', 'active', 'versions',
                'ownership', 'origin', 'license', 'license_materials', 'distribution_status', 'dependencies'}:
            raise ResourceError('resource_incompatible', '资源组件响应字段无效。')
        component_id(item['id'])
        for key in ('title', 'version', 'origin', 'license', 'distribution_status'):
            bounded_string(item[key], 4096)
        if (item['ownership'] != 'application_managed' or not isinstance(item['dependencies'], list) or
                len(item['dependencies']) > 8 or not isinstance(item['license_materials'], list) or
                len(item['license_materials']) > 32):
            raise ResourceError('resource_incompatible', '资源组件归属或依赖响应无效。')
        for dependency in item['dependencies']:
            component_id(dependency)
        for path in item['license_materials']:
            bounded_string(path, 4096)
        if not isinstance(item.get('versions'), list) or len(item['versions']) > 64:
            raise ResourceError('resource_limit', '受控版本列表无效。')
        instances = set()
        for row in item['versions']:
            if (not isinstance(row, dict) or set(row) != {'instance', 'path', 'state', 'active'} or
                    not isinstance(row['instance'], str) or not re.fullmatch('[0-9a-f]{32}', row['instance']) or
                    row['instance'] in instances or not isinstance(row['path'], str) or
                    not Path(row['path']).is_absolute() or len(row['path'].encode()) > 4096 or
                    row['state'] not in ('present', 'missing', 'ownership_unconfirmed') or
                    type(row['active']) is not bool or row['active'] != (row['instance'] == item['active'])):
                raise ResourceError('resource_incompatible', '受控版本响应身份无效。')
            instances.add(row['instance'])
        if item['active'] is not None and item['active'] not in instances:
            raise ResourceError('resource_incompatible', '资源活动版本响应缺失。')
    ids = [item['id'] for item in value['components']]
    if len(set(ids)) != len(ids):
        raise ResourceError('resource_incompatible', '资源组件响应重复。')
    for row in value['pending']:
        if (not isinstance(row, dict) or set(row) != {'id', 'stage', 'component'} or
                not isinstance(row['id'], str) or not re.fullmatch('[0-9a-f]{32}', row['id']) or
                row['stage'] not in (*TERMINAL, 'started', 'copying', 'rebuilding', 'verifying',
                                    'prepared', 'activating', 'deleting')):
            raise ResourceError('resource_incompatible', '中断事务响应无效。')
        if row['component'] is not None:
            component_id(row['component'])
    if (not isinstance(value['root'], str) or not Path(value['root']).is_absolute() or
            len(value['root'].encode()) > 4096 or value['catalog_id'] is not None and
            (not isinstance(value['catalog_id'], str) or not re.fullmatch('[0-9a-f]{64}', value['catalog_id'])) or
            value['warning'] is not None and (not isinstance(value['warning'], str) or len(value['warning'].encode()) > 4096)):
        raise ResourceError('resource_incompatible', '资源响应根或清单身份无效。')
    if value['current'] is not None:
        current = value['current']
        required = {'version','id','owner','action','component','instance','stage','started_at','updated_at',
                    'copy_bytes','hash_bytes','error','confirmed'}
        optional = {'record','elapsed_seconds','reused'}
        if (not isinstance(current, dict) or not required <= set(current) or set(current)-required-optional or
                current['version'] != TRANSACTION or current['action'] not in ACTIONS or
                current['stage'] not in (*TERMINAL, 'started','copying','rebuilding','verifying','prepared','activating','deleting') or
                not isinstance(current['id'], str) or not re.fullmatch('[0-9a-f]{32}', current['id']) or
                type(current['confirmed']) is not bool or
                any(type(current[k]) is not int or not 0 <= current[k] <= SET_LIMIT for k in ('copy_bytes','hash_bytes'))):
            raise ResourceError('resource_incompatible', '当前事务响应字段无效。')
        owner = current['owner']
        if (not isinstance(owner, dict) or set(owner) != {'worker_session','request_id','operation_id',
                'configuration_revision','selection_revision','resource_set_id','generation'} or
                any(type(owner[k]) is not int or not 0 <= owner[k] < 2**53 for k in
                    ('configuration_revision','selection_revision','generation'))):
            raise ResourceError('resource_incompatible', '资源事务归属字段无效。')
        try:
            for key in ('worker_session','request_id','operation_id'):
                uuid.UUID(owner[key])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ResourceError('resource_incompatible', '资源事务归属身份无效。') from exc
        if (owner['operation_id'].replace('-', '') != current['id'] or
                owner['resource_set_id'] is not None and (not isinstance(owner['resource_set_id'], str) or
                not re.fullmatch('[0-9a-f]{64}', owner['resource_set_id']))):
            raise ResourceError('resource_incompatible', '资源事务集合或操作身份无效。')
        if current['component'] is not None:
            component_id(current['component'])
        if current['instance'] is not None and (not isinstance(current['instance'], str) or
                not re.fullmatch('[0-9a-f]{32}', current['instance'])):
            raise ResourceError('resource_incompatible', '资源事务版本身份无效。')
        if any(type(current[k]) not in (float, int) or current[k] < 0 for k in ('started_at','updated_at')):
            raise ResourceError('resource_incompatible', '资源事务时间字段无效。')
        if current['error'] is not None:
            if not isinstance(current['error'], dict) or set(current['error']) != {'code','message'}:
                raise ResourceError('resource_incompatible', '资源事务错误字段无效。')
            bounded_string(current['error']['code'], 128)
            bounded_string(current['error']['message'], 4096)
        if 'record' in current:
            ManagedResources._validate_record(current['record'].get('instance'), current['record'])
    return value
