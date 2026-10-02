import asyncio
from types import SimpleNamespace
import wave

import pytest

from streaming_translator.__main__ import execute
from streaming_translator.capture import PCMArchive
from streaming_translator.store import SessionStore


@pytest.mark.parametrize('failed_path', ['snapshot', 'metadata'])
def test_background_storage_failure_stops_idle_capture_and_cannot_report_recorded(tmp_path, monkeypatch, failed_path):
    """The volume trial found an idle recorder outliving its failed publisher."""
    config = tmp_path / 'config.json'
    config.write_text('{}')
    store = SessionStore(tmp_path / 'session')
    args = SimpleNamespace(command='record', config=config, no_translation=True, translation_model='unused')
    update, publish = store.update, store.publish
    started = asyncio.Event()
    cancelled = []

    async def idle_receive(self):
        with wave.open(str(self.path), 'wb') as media:
            media.setnchannels(1)
            media.setsampwidth(2)
            media.setframerate(16000)
            media.writeframes(b'')
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
            self.done.set()

    def failing_publish(*a, **kw):
        if failed_path == 'snapshot' and started.is_set():
            raise OSError(28, 'No space left on device')
        return publish(*a, **kw)

    def failing_update(**values):
        if failed_path == 'metadata' and 'worker_pid' in values:
            raise OSError(28, 'database or disk is full')
        return update(**values)

    monkeypatch.setattr(PCMArchive, 'receive', idle_receive)
    monkeypatch.setattr(store, 'publish', failing_publish)
    monkeypatch.setattr(store, 'update', failing_update)
    try:
        if failed_path == 'snapshot':
            with pytest.raises(OSError, match='space'):
                asyncio.run(asyncio.wait_for(execute(args, store), timeout=3))
        else:
            assert asyncio.run(asyncio.wait_for(execute(args, store), timeout=3)) == 1
        assert cancelled and store.get('state') == 'failed'
        assert '保存失败' in store.get('error')
        assert store.get('asr_complete') is False
    finally:
        store.close()
