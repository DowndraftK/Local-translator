"""SQLite is authoritative; JSON snapshots are replaceable UI projections."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .segmentation import take_segments


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class SessionStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'session.sqlite', timeout=10)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > 2:
            self.db.close()
            raise ValueError('任务数据库版本较新，请使用更新的应用。')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS asr_events (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1,
                start REAL NOT NULL, end REAL NOT NULL, english TEXT NOT NULL,
                boundary TEXT NOT NULL, committed_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS source_revisions (
                segment_id INTEGER, revision INTEGER, english TEXT NOT NULL,
                changed_at REAL NOT NULL, PRIMARY KEY(segment_id, revision));
            CREATE TABLE IF NOT EXISTS translations (
                segment_id INTEGER PRIMARY KEY REFERENCES segments(id), revision INTEGER NOT NULL,
                state TEXT NOT NULL, chinese TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                lease TEXT, started_at REAL, completed_at REAL, model TEXT,
                result_json TEXT);
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY, sample INTEGER NOT NULL, sequence INTEGER NOT NULL,
                segment_id INTEGER NOT NULL, pending_tokens TEXT NOT NULL,
                media_digest TEXT NOT NULL, resources_digest TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery_history (
                id INTEGER PRIMARY KEY, created_at REAL NOT NULL, checkpoint_id INTEGER,
                payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS translation_state ON translations(state, segment_id);
        ''')
        if version < 2:
            self.db.execute('PRAGMA user_version=2')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, json.dumps(value, ensure_ascii=False)))

    def update(self, **values):
        with self.transaction():
            for key, value in values.items():
                self.set(key, value)

    def initialize(self, **metadata):
        if self.get('session_id'):
            raise ValueError('任务目录已有数据；请打开任务补译，或为新识别选择新目录。')
        self.update(session_id=str(uuid.uuid4()), created_at=time.time(), state='preparing',
                    pending_tokens=[], asr_complete=False, **metadata)

    def _add_segment(self, segment):
        cursor = self.db.execute('INSERT INTO segments(start,end,english,boundary,committed_at) VALUES (?,?,?,?,?)',
                                (segment['start'], segment['end'], segment['english'], segment['boundary'], time.time()))
        index = cursor.lastrowid
        self.db.execute('INSERT INTO source_revisions VALUES (?,?,?,?)', (index, 1, segment['english'], time.time()))
        self.db.execute('INSERT INTO translations(segment_id,revision,state,model) VALUES (?,1,?,?)',
                        (index, 'pending' if self.get('translation_model') else 'disabled', self.get('translation_model')))
        return index

    def ingest(self, sequence, tokens, final=False, complete=None):
        event = {'tokens': tokens, 'final': final}
        if complete is not None:
            event['complete'] = complete
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.transaction():
            previous = self.db.execute('SELECT payload FROM asr_events WHERE sequence=?', (sequence,)).fetchone()
            if previous:
                if previous[0] != payload:
                    raise ValueError('同一 ASR 事件序号的内容发生冲突。')
                return []
            last = self.db.execute('SELECT MAX(sequence) FROM asr_events').fetchone()[0]
            if sequence != (0 if last is None else last + 1):
                raise ValueError('ASR 事件顺序不连续。')
            self.db.execute('INSERT INTO asr_events VALUES (?,?)', (sequence, payload))
            segments, pending = take_segments(self.get('pending_tokens', []) + tokens, final=final)
            ids = [self._add_segment(segment) for segment in segments]
            self.set('pending_tokens', pending)
            if final:
                self.set('asr_complete', True if complete is None else complete)
            return ids

    def next_sequence(self):
        last = self.db.execute('SELECT MAX(sequence) FROM asr_events').fetchone()[0]
        return 0 if last is None else last + 1

    def checkpoint(self, sample, media_digest, resources_digest):
        with self.transaction():
            last = self.db.execute('SELECT MAX(sample) FROM checkpoints').fetchone()[0] or 0
            if sample <= last:
                return
            self.db.execute('INSERT INTO checkpoints(sample,sequence,segment_id,pending_tokens,media_digest,resources_digest,created_at) VALUES (?,?,?,?,?,?,?)',
                (sample, self.next_sequence()-1,
                 self.db.execute('SELECT COALESCE(MAX(id),0) FROM segments').fetchone()[0],
                 json.dumps(self.get('pending_tokens', [])), media_digest, resources_digest, time.time()))

    def latest_checkpoint(self):
        row = self.db.execute('SELECT * FROM checkpoints ORDER BY sample DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def restart_from_checkpoint(self, checkpoint):
        # The caller validates media and resource digests BEFORE this transaction.
        sample = checkpoint['sample'] if checkpoint else 0
        sequence = checkpoint['sequence'] if checkpoint else -1
        segment_id = checkpoint['segment_id'] if checkpoint else 0
        with self.transaction():
            tail = {table: [dict(row) for row in self.db.execute(query, (bound,))] for table, query, bound in [
                ('segments', 'SELECT * FROM segments WHERE id>?', segment_id),
                ('translations', 'SELECT * FROM translations WHERE segment_id>?', segment_id),
                ('source_revisions', 'SELECT * FROM source_revisions WHERE segment_id>?', segment_id),
                ('asr_events', 'SELECT * FROM asr_events WHERE sequence>?', sequence)]}
            tail['metadata'] = {row['key']: json.loads(row['value']) for row in self.db.execute('SELECT * FROM metadata')}
            self.db.execute('INSERT INTO recovery_history(created_at,checkpoint_id,payload) VALUES (?,?,?)',
                (time.time(), checkpoint['id'] if checkpoint else None, json.dumps(tail, ensure_ascii=False)))
            for table in ('translations', 'source_revisions'):
                self.db.execute(f'DELETE FROM {table} WHERE segment_id>?', (segment_id,))
            self.db.execute('DELETE FROM segments WHERE id>?', (segment_id,))
            self.db.execute('DELETE FROM asr_events WHERE sequence>?', (sequence,))
            self.set('pending_tokens', json.loads(checkpoint['pending_tokens']) if checkpoint else [])
            self.set('asr_complete', False)
            self.set('resume_sample', sample)
            self.set('error', None)
            self.set('asr_error', None)
            self.set('state', 'preparing')
            self.set('stopped', False)
        self.recover(retry_failed=True)
        return sample

    def claim(self):
        with self.transaction():
            row = self.db.execute('''SELECT s.id,s.revision,s.english FROM segments s JOIN translations t
                ON t.segment_id=s.id AND t.revision=s.revision WHERE t.state='pending' ORDER BY s.id LIMIT 1''').fetchone()
            if not row:
                return None
            lease = str(uuid.uuid4())
            self.db.execute("UPDATE translations SET state='running',lease=?,started_at=?,attempts=attempts+1,error=NULL WHERE segment_id=?",
                            (lease, time.time(), row['id']))
            return {**dict(row), 'lease': lease}

    def finish(self, job, result=None, error=None):
        with self.transaction():
            # A corrected source or a retried job cannot accept the previous request's result.
            cursor = self.db.execute('''UPDATE translations SET state=?,chinese=?,error=?,completed_at=?,result_json=?,lease=NULL
                WHERE segment_id=? AND revision=? AND lease=? AND state='running'
                AND revision=(SELECT revision FROM segments WHERE id=?)''',
                ('failed' if error else 'completed', None if error else result['translation'], error, time.time(),
                 json.dumps(result, ensure_ascii=False) if result else None,
                 job['id'], job['revision'], job['lease'], job['id']))
            return cursor.rowcount == 1

    def recover(self, retry_failed=False):
        with self.transaction():
            states = "'running','failed'" if retry_failed else "'running'"
            self.db.execute(f"UPDATE translations SET state='pending',lease=NULL,error=NULL WHERE state IN ({states})")

    def revise(self, segment_id, english):
        english = english.strip()
        if not english or len(english) > 8000:
            raise ValueError('请输入 1–8000 字符的原文。')
        with self.transaction():
            row = self.db.execute('SELECT revision,english FROM segments WHERE id=?', (segment_id,)).fetchone()
            if not row:
                raise ValueError('片段不存在。')
            if row['english'] == english:
                return
            revision = row['revision'] + 1
            self.db.execute('UPDATE segments SET english=?,revision=? WHERE id=?', (english, revision, segment_id))
            self.db.execute('INSERT INTO source_revisions VALUES (?,?,?,?)', (segment_id, revision, english, time.time()))
            self.db.execute("UPDATE translations SET revision=?,state=?,chinese=NULL,error=NULL,lease=NULL,result_json=NULL,completed_at=NULL WHERE segment_id=?",
                            (revision, 'pending' if self.get('translation_model') else 'disabled', segment_id))
            self.set('state', 'needs_translation' if self.get('translation_model') else self.get('state'))

    def snapshot(self, limit=None, offset=None):
        total = self.db.execute('SELECT COUNT(*) FROM segments').fetchone()[0]
        offset = max(0, total-limit) if limit is not None and offset is None else max(0, offset or 0)
        rows = self.db.execute('''SELECT s.*,t.state AS translation_state,t.chinese,t.error AS translation_error,
            t.attempts,t.started_at AS translation_started_at,t.completed_at AS translated_at
            FROM segments s JOIN translations t ON t.segment_id=s.id ORDER BY s.id LIMIT ? OFFSET ?''',
            (-1 if limit is None else limit, offset)).fetchall()
        metadata = {row['key']: json.loads(row['value']) for row in self.db.execute('SELECT * FROM metadata')}
        pending = metadata.pop('pending_tokens', [])
        metadata.update(segment_count=total, segment_offset=offset, segments=[dict(row) for row in rows],
                        pending_english=''.join(t['text'] for t in pending).strip(),
                        updated_at=time.time())
        counts = dict(self.db.execute('SELECT state,COUNT(*) FROM translations GROUP BY state'))
        metadata['translation_counts'] = {s: counts.get(s, 0) for s in ['pending', 'running', 'completed', 'failed', 'disabled']}
        metadata['checkpoint_sample'] = self.db.execute('SELECT COALESCE(MAX(sample),0) FROM checkpoints').fetchone()[0]
        return metadata

    def publish(self, limit=200, offset=None):
        request = self.directory / 'view.json'
        if offset is None and request.exists():
            try:
                offset = json.loads(request.read_text()).get('offset')
                if offset is not None:
                    offset = max(0, int(offset))
            except (ValueError, OSError, TypeError):
                offset = None
        snapshot = self.snapshot(limit=limit, offset=offset)
        atomic_json(self.directory / 'snapshot.json', snapshot)
        return snapshot


def export_subtitles(snapshot, format='srt'):
    if format not in ('srt', 'vtt', 'txt'):
        raise ValueError('导出格式必须是 srt、vtt 或 txt。')
    if format == 'txt':
        return '\n\n'.join(f"[{r['start']:.2f}–{r['end']:.2f}]\n{r['english']}\n{r['chinese'] or '［尚未翻译］'}" for r in snapshot['segments']) + '\n'
    def stamp(seconds):
        value = max(0, round(seconds * 1000))
        hours, value = divmod(value, 3600000)
        minutes, value = divmod(value, 60000)
        seconds, millis = divmod(value, 1000)
        return f'{hours:02}:{minutes:02}:{seconds:02}{"," if format == "srt" else "."}{millis:03}'
    def clean(text):
        # Prevent source text from injecting subtitle markup or cue separators.
        return ' '.join((text or '［尚未翻译］').split()).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    output = ['WEBVTT\n'] if format == 'vtt' else []
    previous_end = 0
    for i, row in enumerate(snapshot['segments'], 1):
        start = max(previous_end, row['start'], 0)
        end = max(start + .1, row['end'])
        previous_end = end
        output.append(f"{i}\n{stamp(start)} --> {stamp(end)}\n{clean(row['english'])}\n{clean(row['chinese'])}\n")
    return '\n'.join(output) + '\n'
