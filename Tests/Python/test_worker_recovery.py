"""Crash only a test-owned translation worker; the real Ollama is never touched."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from streaming_translator.store import SessionStore


def wait_until(check, seconds=12):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.05)
    raise AssertionError('Worker did not reach expected durable state')


def test_killed_worker_recovers_only_unfinished_translation(tmp_path):
    blocked = threading.Event()
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, data, newline=False):
            payload = (json.dumps(data, ensure_ascii=False)+('\n' if newline else '')).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson' if newline else 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.respond({'models': [{'name': 'fixture', 'digest': 'test-model-digest'}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/api/show':
                self.respond({'capabilities': ['completion']}); return
            prompt = body['messages'][0]['content']
            name = 'second' if 'Second sentence' in prompt else 'first'
            requests.append(name)
            if name == 'second' and not release.is_set():
                blocked.set(); release.wait(12)
            self.respond({'message': {'content': '第二句' if name == 'second' else '第一句'},
                          'done': True, 'done_reason': 'stop', 'eval_count': 3}, newline=True)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    store = SessionStore(tmp_path/'session')
    store.initialize(translation_model='fixture', endpoint=f'http://127.0.0.1:{server.server_port}')
    store.ingest(0, [{'text': 'First sentence.', 'start': 0, 'end': 1},
                     {'text': ' Second sentence.', 'start': 1, 'end': 2}], final=True)
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]/'runtime'), PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-m', 'streaming_translator', 'retry', '--session', str(store.directory), '--retry-failed']
    child = None
    try:
        with (tmp_path/'first.log').open('w') as log:
            child = subprocess.Popen(command, env=environment, stdout=log, stderr=log)
            wait_until(lambda: blocked.is_set() and store.snapshot()['translation_counts']['completed'] == 1)
            before = store.snapshot()
            assert before['segments'][0]['english'] == 'First sentence.'
            assert before['segments'][1]['translation_state'] == 'running'
            child.kill(); child.wait(timeout=5)
        release.set()
        with (tmp_path/'recovered.log').open('w') as log:
            result = subprocess.run(command, env=environment, stdout=log, stderr=log, timeout=12)
        assert result.returncode == 0
        after = store.snapshot()
        assert after['state'] == 'completed'
        assert after['translation_counts']['completed'] == 2
        assert after['segments'][0]['attempts'] == 1
        assert after['segments'][1]['attempts'] == 2
        assert requests.count('first') == 1 and requests.count('second') == 2
        assert [s['english'] for s in after['segments']] == ['First sentence.', 'Second sentence.']
        assert (store.directory/'subtitles.srt').exists()
    finally:
        release.set()
        if child and child.poll() is None:
            child.kill(); child.wait(timeout=5)
        store.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
