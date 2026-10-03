import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from streaming_translator.capture import resource_digest
from streaming_translator.resources import resolve_config, validate_assets, _scope_os_asset_directory
from streaming_translator.store import file_sha256


def test_release_trim_accounts_for_sealed_test_tools_without_weakening_identity(tmp_path, monkeypatch):
    from streaming_translator.resources import validate_source_files
    root = tmp_path/'managed'; source=root/'Code/source'; source.mkdir(parents=True)
    keep='whisperlivekit/core.py'; omitted='whisperlivekit/test_harness.py'
    (source/keep).parent.mkdir(); (source/keep).write_text('fixed execution code')
    test_file=tmp_path/'test_tool';test_file.write_text('development-only test harness')
    fingerprints={'patched_files':{keep:file_sha256(source/keep),omitted:file_sha256(test_file)}}
    manifest=root/'Code/pinned-source.json';manifest.write_text(json.dumps(fingerprints))
    release=root/'Code/release-source.json'
    release.write_text(json.dumps({'original_manifest_sha256':file_sha256(manifest),'omitted_test_tools':{omitted:fingerprints['patched_files'][omitted]}}))
    component=root/'component.json';component.write_text(json.dumps({'release_source_sha256':file_sha256(release)}))
    monkeypatch.setenv('LOCAL_TRANSLATOR_RUNTIME_ROOT',str(root))
    validate_source_files(source,fingerprints)
    original=manifest.read_bytes()
    (source/keep).write_text('modified execution code')
    with pytest.raises(ValueError,match='源码校验'):validate_source_files(source,fingerprints)
    release.write_text(json.dumps({'original_manifest_sha256':file_sha256(manifest),'omitted_test_tools':{keep:fingerprints['patched_files'][keep]}}))
    component.write_text(json.dumps({'release_source_sha256':file_sha256(release)}))
    with pytest.raises(ValueError,match='差异不合法'):validate_source_files(source,fingerprints)
    assert manifest.read_bytes()==original


def layout(tmp_path, monkeypatch):
    old = tmp_path/'old'; old.mkdir()
    root = tmp_path/'managed'; (root/'Code').mkdir(parents=True)
    for name, content in [('pinned-source.json',{'patched_files':{'a.py':'a'}}),
                          ('model-manifest.json',{'revision':'fixed','files':{}})]:
        (old/name).write_text(json.dumps(content))
        (root/'Code'/name).write_text(json.dumps(content))
    monkeypatch.setenv('LOCAL_TRANSLATOR_RUNTIME_ROOT',str(root))
    monkeypatch.setenv('LOCAL_TRANSLATOR_MODEL_PATH',str(tmp_path/'relocated-model'))
    config={'source':str(old/'source'),'source_manifest':str(old/'pinned-source.json'),
            'model':str(old/'model'),'model_manifest':str(old/'model-manifest.json'),
            'device':'mps','dtype':'float32','max_context_tokens':128}
    return config, root, old


def test_content_equal_relocation_keeps_checkpoint_digest_and_original_config(tmp_path,monkeypatch):
    config, root, old=layout(tmp_path,monkeypatch)
    expected=resource_digest(config); original=dict(config)
    moved=resolve_config(config,expected)
    assert config==original and moved['source']!=config['source']
    assert resource_digest(moved)==expected
    for p in old.glob('*.json'):p.unlink()
    assert resource_digest(resolve_config(config,expected))==expected
    with pytest.raises(ValueError,match='身份证据'):resolve_config(config)


def test_path_mapping_cannot_bypass_behavior_or_manifest_mismatch(tmp_path,monkeypatch):
    config, root, old=layout(tmp_path,monkeypatch); expected=resource_digest(config)
    with pytest.raises(ValueError,match='指纹不配'):resolve_config({**config,'dtype':'float16'},expected)
    (old/'pinned-source.json').write_text(json.dumps({'patched_files':{'a.py':'wrong'}}))
    with pytest.raises(ValueError,match='清单'):resolve_config(config,expected)


def test_permission_denied_old_manifest_requires_matching_saved_identity(tmp_path,monkeypatch):
    config, root, old=layout(tmp_path,monkeypatch)
    expected=resource_digest(config); original=Path.read_text
    def restricted_read(path, *args, **kwargs):
        if path.parent == old:
            raise PermissionError('old installation is inaccessible')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', restricted_read)
    assert resource_digest(resolve_config(config,expected)) == expected
    with pytest.raises(ValueError,match='身份证据'):resolve_config(config)
    with pytest.raises(ValueError,match='指纹不配'):resolve_config({**config,'dtype':'float16'},expected)


def test_refinement_asset_mapping_does_not_load_streaming_frameworks(tmp_path,monkeypatch):
    import streaming_translator.resources as resources
    imported=[]
    monkeypatch.setattr(resources,'validate_assets',lambda: tmp_path)
    def module(name):
        imported.append(name)
        return SimpleNamespace(__file__='/managed/'+name+'.py', os=os)
    monkeypatch.setattr(resources.importlib,'import_module',module)
    resources.apply_asset_mapping('refinement')
    assert imported == ['mlx_whisper.audio','mlx_whisper.tokenizer']


def test_external_asset_content_is_verified_before_load(tmp_path,monkeypatch):
    _,root,_=layout(tmp_path,monkeypatch); assets=tmp_path/'assets'; assets.mkdir()
    file=assets/'vocab';file.write_bytes(b'original-vocabulary')
    (root/'Code/assets-manifest.json').write_text(json.dumps({'files':{'vocab':{'size':file.stat().st_size,'sha256':file_sha256(file)}}}))
    monkeypatch.setenv('LOCAL_TRANSLATOR_ASSETS_ROOT',str(assets))
    assert validate_assets()==assets
    file.write_bytes(b'wrong-vocabulary')
    with pytest.raises(ValueError,match='校验失败'):validate_assets()


def test_asset_shim_preserves_code_origin_and_all_other_path_lookups(tmp_path):
    module=SimpleNamespace(__file__='/managed/sealed/tokenizer.py',os=os)
    _scope_os_asset_directory(module,tmp_path)
    assert module.__file__=='/managed/sealed/tokenizer.py'
    assert module.os.path.dirname(module.__file__)==str(tmp_path)
    assert module.os.path.dirname('/elsewhere/file')=='/elsewhere'
    assert os.path.dirname(module.__file__)=='/managed/sealed'
