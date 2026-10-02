import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from streaming_translator.ollama import LocalTranslator
from streaming_translator.store import SessionStore
from streaming_translator.translation_config import recipe, saved_recipe, binding


def test_evaluation_and_runtime_recipes_are_the_same():
    path = Path(__file__).parents[2]/'scripts/translation-quality-eval.py'
    spec = importlib.util.spec_from_file_location('quality_eval', path)
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    for legacy in (True, False):
        expected = runner.recipe('hy-mt2:7b-q8', 'en-zh', candidate=not legacy)
        actual = recipe('hy-mt2:7b-q8', legacy=legacy)
        assert actual['user_prefix'] == expected['user_prefix']
        assert actual['options'] == expected['options']


def test_old_session_continues_v1_and_completed_results_survive(tmp_path):
    store = SessionStore(tmp_path)
    store.initialize(translation_model='hy-mt2:1.8b-q8', endpoint='http://127.0.0.1:11434')
    store.ingest(0, [{'text':'Old first.', 'start':0, 'end':1},
                     {'text':' New second.', 'start':1, 'end':2}], final=True)
    first = store.claim(); store.finish(first, result={'translation':'原成功译文', 'prompt_profile':'hy-mt2-faithful-v1'})
    before = dict(store.db.execute('SELECT * FROM translations WHERE segment_id=1').fetchone())
    store.close(); store = SessionStore(tmp_path)
    config = saved_recipe(store)
    assert config['version'] == 1 and config['options']['temperature'] == .7
    config['model_digest'] = 'legacy-digest'; store.bind_translation_configuration(config)
    store.recover(retry_failed=True)
    assert dict(store.db.execute('SELECT * FROM translations WHERE segment_id=1').fetchone()) == before
    assert store.claim()['id'] == 2
    with pytest.raises(ValueError, match='另一套'):
        store.bind_translation_configuration(recipe('hy-mt2:1.8b-q8', digest='legacy-digest'))
    store.close()


def test_config_change_and_wrong_result_cannot_accept_old_lease(tmp_path):
    store = SessionStore(tmp_path)
    config = recipe(digest='digest')
    store.initialize(translation_model=config['model'], translation_configuration=config)
    store.ingest(0, [{'text':'Must remain.', 'start':0, 'end':1}], final=True)
    job=store.claim()
    assert not store.finish(job, result={'translation':'错误配置', 'configuration_binding':'wrong'})
    changed = {**config, 'options':{**config['options'], 'seed':43}}
    store.update(translation_configuration=changed)
    assert not store.finish(job, result={'translation':'旧响应', 'configuration_binding':binding(config)})
    assert store.snapshot()['segments'][0]['english']=='Must remain.'
    store.close()


@pytest.mark.parametrize('failure', ['truncated', 'empty', 'digest', 'budget', 'future'])
def test_request_guards_keep_failed_output_out_of_success(failure):
    async def run():
        config=recipe('local', digest='expected' if failure=='digest' else None)
        if failure=='future': config['version']=99
        if failure=='future':
            with pytest.raises(ValueError, match='不兼容'): LocalTranslator('http://127.0.0.1', 'local', config)
            return
        client=LocalTranslator('http://127.0.0.1', 'local', config); calls=[]
        async def handler(request):
            calls.append(request.url.path)
            if request.url.path=='/api/tags':
                return httpx.Response(200,json={'models':[{'name':'local','digest':'actual'}]})
            if request.url.path=='/api/show':
                return httpx.Response(200,json={'capabilities':['completion']})
            body=json.loads(request.content)
            assert body['options']['seed']==42 and body['options']['num_predict']==4096
            return httpx.Response(200,text=json.dumps({'message':{'content':'' if failure=='empty' else 'partial'},
                    'done':True,'done_reason':'length' if failure=='truncated' else 'stop'})+'\n')
        await client.client.aclose()
        client.client=httpx.AsyncClient(base_url=client.endpoint,transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ValueError):
                await client.translate('原'*700 if failure=='budget' else 'Original.')
            if failure in ('digest','budget'): assert '/api/chat' not in calls
        finally: await client.close()
    asyncio.run(run())
