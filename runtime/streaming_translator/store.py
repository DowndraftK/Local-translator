"""SQLite is authoritative; JSON snapshots are replaceable UI projections."""
from contextlib import contextmanager
import hashlib
import json
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
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
    temporary.replace(path)


class SessionStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'session.sqlite', timeout=10)
        self.db.row_factory = sqlite3.Row
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
        ''')

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

    def ingest(self, sequence, tokens, final=False):
        payload = json.dumps({'tokens': tokens, 'final': final}, ensure_ascii=False, sort_keys=True)
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
                self.set('asr_complete', True)
            return ids

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

    def snapshot(self):
        rows = self.db.execute('''SELECT s.*,t.state AS translation_state,t.chinese,t.error AS translation_error,
            t.attempts,t.started_at AS translation_started_at,t.completed_at AS translated_at
            FROM segments s JOIN translations t ON t.segment_id=s.id ORDER BY s.id''').fetchall()
        metadata = {row['key']: json.loads(row['value']) for row in self.db.execute('SELECT * FROM metadata')}
        pending = metadata.pop('pending_tokens', [])
        metadata.update(segments=[dict(row) for row in rows],
                        pending_english=''.join(t['text'] for t in pending).strip(),
                        updated_at=time.time())
        metadata['translation_counts'] = {s: sum(r['translation_state'] == s for r in rows)
                                          for s in ['pending', 'running', 'completed', 'failed', 'disabled']}
        return metadata

    def publish(self):
        snapshot = self.snapshot()
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
