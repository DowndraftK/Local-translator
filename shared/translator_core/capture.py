"""Versioned growing-recording facts. This module never opens an audio device."""
import math

from . import CoreError
from .audio import MAX_SAMPLES, integer
from .contracts import task_id
from .resources import hash_value

CONTRACT = 'audio-recording-v1'
SOURCE = 'audio-capture-source-v1'
FEED = 'audio-capture-feed-v1'
KINDS = ('microphone', 'system_audio')
STATES = ('preparing', 'waiting_permission', 'starting', 'recording', 'pausing', 'paused',
          'stopping', 'finalizing', 'sealed', 'interrupted', 'failed', 'save_blocked')
MAX_CHUNKS = 3600
CHUNK_SAMPLES = 8000


def validate_source(source):
    if (not isinstance(source, dict) or source.get('contract') != SOURCE or source.get('kind') not in KINDS or
            not integer(source.get('generation'), 1, 2**31) or not isinstance(source.get('name'), str) or
            not 0 < len(source['name'].encode()) <= 1024 or not isinstance(source.get('permission'), dict)):
        raise CoreError('采集来源身份或版本无效。', 'capture_source')
    task_id(source.get('id'))
    if source['kind'] == 'microphone' and (not isinstance(source.get('deviceUID'), str) or
            not 0 < len(source['deviceUID'].encode()) <= 1024):
        raise CoreError('选定麦克风UID无效。', 'capture_source')
    if source['kind'] == 'system_audio' and source.get('scope') != 'all_capturable_system_audio':
        raise CoreError('系统声音捕获范围无效。', 'capture_source')


def validate_recording(value):
    if (not isinstance(value, dict) or value.get('contract') != CONTRACT or value.get('state') not in STATES or
            not isinstance(value.get('owner'), dict) or not isinstance(value.get('epochs'), list) or
            len(value['epochs']) > 64 or not isinstance(value.get('events'), list) or len(value['events']) > 256):
        raise CoreError('录音清单版本或状态无效。', 'future_schema')
    task_id(value.get('session'))
    validate_source(value.get('source'))
    for field in ('worker', 'request', 'operation', 'lease', 'task'):
        task_id(value['owner'].get(field))
    water = value.get('watermarks')
    if not isinstance(water, dict) or any(not integer(water.get(k), 0, MAX_SAMPLES) for k in
            ('received', 'durable', 'asrConsumed', 'stable', 'translated', 'finalized')):
        raise CoreError('录音水位字段无效。', 'capture_watermark')
    if not (water['finalized'] <= water['durable'] <= water['received'] and
            water['asrConsumed'] <= water['durable'] and water['translated'] <= water['stable'] <= water['durable']):
        raise CoreError('录音水位顺序无效。', 'capture_watermark')
    chunks = value.get('chunks')
    if not isinstance(chunks, list) or len(chunks) > MAX_CHUNKS:
        raise CoreError('录音分块超过容量。', 'capture_limit')
    offset = 0
    for index, chunk in enumerate(chunks):
        if (not isinstance(chunk, dict) or type(chunk.get('sequence')) is not int or chunk['sequence'] != index or
                type(chunk.get('offset')) is not int or chunk['offset'] != offset or
                not integer(chunk.get('samples'), 1, CHUNK_SAMPLES) or chunk.get('bytes') != chunk['samples'] * 2 or
                chunk.get('file') != f'capture/{index:06d}.pcm' or
                not integer(chunk.get('epoch'), 1, 64)):
            raise CoreError('录音分块顺序、范围或路径无效。', 'capture_chunk')
        hash_value(chunk.get('sha256'))
        offset += chunk['samples']
    if offset != water['durable']:
        raise CoreError('录音清单与已落盘样本不符。', 'capture_watermark')
    if value['state'] == 'sealed' and water['finalized'] != water['durable']:
        raise CoreError('录音封存水位未确认。', 'capture_watermark')
    for index, epoch in enumerate(value['epochs'], 1):
        if (not isinstance(epoch, dict) or epoch.get('epoch') != index or
                not integer(epoch.get('startSample'), 0, water['durable'])):
            raise CoreError('录音epoch无效。', 'capture_epoch')
        if 'endSample' in epoch and not integer(epoch['endSample'], epoch['startSample'], water['durable']):
            raise CoreError('录音epoch边界无效。', 'capture_epoch')
    if not integer(value.get('activeEpoch'), 0, len(value['epochs'])):
        raise CoreError('当前录音epoch无效。', 'capture_epoch')
    for event in value['events']:
        if (not isinstance(event, dict) or event.get('kind') not in ('paused', 'stopped', 'interrupted') or
                not integer(event.get('sample'), 0, water['durable']) or
                not integer(event.get('epoch'), 1, len(value['epochs'])) or
                type(event.get('wall_time')) not in (int, float) or not math.isfinite(event['wall_time'])):
            raise CoreError('录音边界事件无效。', 'capture_epoch')
        task_id(event.get('id'))
    return water['durable']


def recording(task):
    return (task.get('audio') or {}).get('recording') if task else None
