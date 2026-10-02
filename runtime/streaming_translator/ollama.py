"""Explicit local-only Ollama requests; errors never erase the English source."""
import json
import time
from urllib.parse import urlsplit
from .translation_config import recipe, validate, binding

import httpx


def validate_endpoint(endpoint):
    parts = urlsplit(endpoint)
    if (parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', '::1')
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ('', '/')):
        raise ValueError('翻译服务仅接受本机回环 HTTP 地址。')
    _ = parts.port
    return endpoint.rstrip('/')


class LocalTranslator:
    def __init__(self, endpoint, model, configuration=None):
        self.endpoint = validate_endpoint(endpoint)
        self.model = model
        self.client = httpx.AsyncClient(base_url=self.endpoint, trust_env=False, follow_redirects=False,
                                       timeout=httpx.Timeout(120, connect=5))
        self.configuration = validate(configuration or recipe(model), model)
        self.digest = self.configuration.get('model_digest')
        self.capabilities = []

    async def close(self):
        await self.client.aclose()

    async def verify(self):
        response = await self.client.get('/api/tags')
        response.raise_for_status()
        selected = next((m for m in response.json()['models'] if m['name'] == self.model), None)
        if (not selected or selected.get('remote_host') or selected.get('remote_model')
                or 'cloud' in self.model.lower() or not selected.get('digest')):
            raise ValueError('所选翻译模型未在本机准备好，或不是可验证的本地模型；不会自动下载。')
        response = await self.client.post('/api/show', json={'model': self.model})
        response.raise_for_status()
        detail = response.json()
        if detail.get('remote_host') or detail.get('remote_model') or 'completion' not in detail.get('capabilities', []):
            raise ValueError('无法确认翻译模型支持本地文字生成。')
        if self.digest is not None and self.digest != selected['digest']:
            raise ValueError('任务运行中模型 digest 改变，请以新模型配置创建任务。')
        self.digest, self.capabilities = selected['digest'], detail['capabilities']
        self.configuration['model_digest'] = self.digest
        return self.digest

    async def translate(self, english):
        if not english.strip() or len(english) > 8000:
            raise ValueError('翻译片段必须为 1–8000 字符。')
        # Keep the same byte budget and output reserve as the reading engine.
        if (len(english.encode()) > 2048
                or len(self.configuration['user_prefix'].encode()) + 64 > 1024):
            raise ValueError('字幕原文超出保守容量预算；原文保留，请明确修订或另建文字任务。')
        # Verify each job so a service restart/model replacement cannot bypass local checks.
        await self.verify()
        prompt = self.configuration['user_prefix'] + english
        body = {'model': self.model, 'messages': [{'role': 'user', 'content': prompt}], 'stream': True,
                'keep_alive': self.configuration['keep_alive'], 'options': self.configuration['options']}
        if 'thinking' in self.capabilities:
            body['think'] = False
        started, first, text, complete, final = time.monotonic(), None, '', False, {}
        async with self.client.stream('POST', '/api/chat', json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                if len(line.encode()) > 256000:
                    raise ValueError('翻译响应行过大。')
                data = json.loads(line)
                if data.get('error'):
                    raise ValueError(str(data['error']))
                delta = data.get('message', {}).get('content', '')
                if delta and first is None:
                    first = time.monotonic() - started
                text += delta
                if len(text.encode()) > 256000:
                    raise ValueError('翻译响应过大。')
                if data.get('done'):
                    if data.get('done_reason') != 'stop' or not text.strip():
                        raise ValueError('译文为空或生成被截断，未保存为成功。')
                    complete, final = True, data
                    break
        if not complete:
            raise ValueError('翻译响应中断，未收到正常结束标记。')
        return {'translation': text.strip(), 'model': self.model, 'model_digest': self.digest,
                'elapsed_seconds': time.monotonic()-started, 'first_text_seconds': first,
                'prompt_profile': self.configuration['prompt_profile'], 'generation': body['options'],
                'request_configuration': self.configuration.copy(), 'configuration_binding': binding(self.configuration),
                'output_tokens': final.get('eval_count'), 'load_duration_ns': final.get('load_duration')}
