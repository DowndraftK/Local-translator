"""Versioned audio task facts, integer media clock and complete subtitle export."""
import html
import re
from copy import deepcopy

from . import CoreError
from .contracts import MAX_INPUT_BYTES, STATES, recipe_binding, task_id, trim
from .languages import valid_direction, speech_language
from .resources import parse_binding, hash_value

CONTRACT = 'audio-task-v1'
SOURCE = 'audio-source-v1'
PCM = 'audio-pcm-v1'
RATE = 16000
MAX_SAMPLES = RATE * 1800
REFINE_SAMPLES = RATE * 600
MAX_CUES = 2048
MAX_CUE_BYTES = 2048
MAX_SOURCE_BYTES = 256_000_000
SUFFIXES = ('.wav', '.aiff', '.aif', '.mp3', '.m4a', '.flac', '.ogg')
PHASES = ('saved', 'decoding', 'recognizing', 'refining', 'completed', 'stopped', 'failed')


def integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def validate_audio_task(task):
    a = task.get('audio')
    capture = isinstance(a, dict) and task.get('formatVersion') == 5 and a.get('contract') == 'audio-task-v2'
    if capture:
        from .capture import validate_recording, STATES as CAPTURE_STATES
        available = validate_recording(a.get('recording'))
        phases = (*PHASES, *CAPTURE_STATES)
    else:
        available = 0
        phases = PHASES
        if isinstance(a, dict) and a.get('recording') is not None:
            raise CoreError('采集清单需要音频schema5和audio-task-v2。', 'future_schema')
    if ((not capture and task.get('formatVersion') != 4) or task.get('kind') != '音频' or task.get('text') is not None or
            task.get('document') is not None or task.get('image') is not None or task.get('input') != '' or
            not isinstance(a, dict) or a.get('contract') != ('audio-task-v2' if capture else CONTRACT) or a.get('phase') not in phases or
            not valid_direction(task['configuration'].get('direction'))):
        raise CoreError('音频正文版本或任务字段不兼容。', 'future_schema')
    direction = task['configuration']['direction']
    variant = a.get('asrModel')
    spoken = speech_language(variant, direction) if variant is not None else None
    source = a.get('source'); asset = task.get('asset')
    growing = capture and a.get('pcm') is None
    if growing:
        from .capture import validate_source
        validate_source(source)
        if asset is not None or source != a['recording']['source']:
            raise CoreError('增长录音不能声明最终音频资产。', 'source_mismatch')
    elif (not isinstance(source, dict) or source.get('contract') != SOURCE or not isinstance(asset, dict) or
            source.get('fingerprint') != asset.get('fingerprint') or source.get('byteCount') != asset.get('byteCount') or
            not integer(source.get('byteCount'), 1, MAX_SOURCE_BYTES) or not isinstance(source.get('file'), str) or
            source.get('suffix') not in SUFFIXES):
        raise CoreError('音频来源字段或资产身份无效。', 'source_mismatch')
    task_id(source.get('id'))
    if capture and source['id'] != a['recording']['source']['id']:
        raise CoreError('封存录音与原采集来源身份不符。', 'source_mismatch')
    if not growing: hash_value(source.get('fingerprint'))
    pcm = a.get('pcm')
    if capture and a['recording']['state'] == 'sealed' and not pcm:
        raise CoreError('最终PCM与录音封存状态不符。', 'audio_incomplete')
    if pcm is not None:
        if (not isinstance(pcm, dict) or pcm.get('contract') != PCM or pcm.get('rate') != RATE or
                pcm.get('channels') != 1 or pcm.get('sampleWidth') != 2 or
                type(pcm.get('exactZeroPCM', False)) is not bool or
                not integer(pcm.get('samples'), 1, MAX_SAMPLES) or
                pcm.get('byteCount') != 44 + pcm['samples'] * 2 or
                pcm.get('filename') != 'pcm-' + str(pcm.get('fingerprint')) + '.wav' or
                not isinstance(pcm.get('media'), dict)):
            raise CoreError('规范音频不完整或超过容量。', 'audio_incomplete')
        hash_value(pcm['fingerprint'])
        if capture and pcm['samples'] != available:
            raise CoreError('封存录音与已落盘水位不符。', 'capture_watermark')
        if capture and a['recording']['watermarks']['finalized'] != available:
            raise CoreError('最终PCM与封存样本水位不符。', 'capture_watermark')
        available = pcm['samples']
    bindings = a.get('bindings')
    if not isinstance(bindings, dict): raise CoreError('音频绑定字段无效。', 'audio_binding')
    for capability, binding in bindings.items():
        parsed = parse_binding(binding)
        if (parsed['capability'] != capability or (spoken and capability.startswith('speech.') and
                parsed['execution']['language'] != spoken) or
                parsed.get('speech') and parsed['speech']['model'] != variant):
            raise CoreError('音频能力绑定不匹配。', 'audio_binding')
    if not isinstance(a.get('executions'), list) or len(a['executions']) > 64:
        raise CoreError('音频执行记录超过容量。', 'audio_limit')
    if not isinstance(a.get('history'), list): raise CoreError('音频修订历史无效。')
    temporary = a.get('temporary')
    if temporary is not None:
        import math
        if (not isinstance(temporary, dict) or not isinstance(temporary.get('text'), str) or
                len(temporary['text'].encode()) > 2048 or temporary.get('confirmed') is not False or
                type(temporary.get('truncated')) is not bool or type(temporary.get('observedWall')) not in (int, float) or
                not math.isfinite(temporary['observedWall'])):
            raise CoreError('临时识别假设字段无效。', 'audio_incomplete')
    cues = a.get('cues')
    if not isinstance(cues, list) or len(cues) > MAX_CUES or not isinstance(a.get('asrComplete'), bool):
        raise CoreError('字幕列表或完成标记无效。', 'audio_limit')
    ids = set(); previous = 0; total = 0
    for cue in cues:
        if not isinstance(cue, dict): raise CoreError('字幕行无效。')
        task_id(cue.get('id')); task_id(cue.get('revision'))
        if cue['id'] in ids: raise CoreError('字幕标识重复。')
        ids.add(cue['id'])
        if (not available or not integer(cue.get('startSample'), previous, available - 1) or
                not integer(cue.get('endSample'), cue['startSample'] + 1, available) or
                not isinstance(cue.get('source'), str) or len(cue['source'].encode()) > MAX_CUE_BYTES or
                not isinstance(cue.get('raw'), dict) or type(cue.get('reviewed')) is not bool or
                cue.get('reviewRevision') != (cue['revision'] if cue['reviewed'] else None) or
                cue.get('state') not in STATES):
            raise CoreError('字幕时间、修订或状态无效。', 'audio_time')
        previous = cue['startSample']; total += len(cue['source'].encode())
        if cue['state'] == 'completed':
            result = cue.get('result')
            if (not isinstance(result, dict) or result.get('source') != trim(cue['source']) or
                    result.get('configurationBinding') != recipe_binding(task['configuration']) or
                    result.get('model') != task['configuration']['model'] or result.get('direction') != direction or
                    not isinstance(result.get('translation'), str) or not trim(result['translation']) or
                    cue.get('translationRevision') != cue['revision']):
                raise CoreError('字幕译文与有效修订不匹配。', 'binding_mismatch')
    if total > MAX_INPUT_BYTES: raise CoreError('字幕正文超过1MiB。', 'audio_limit')
    cp = a.get('checkpoint')
    if (not isinstance(cp, dict) or not integer(cp.get('sample'), 0, available) or
            not integer(cp.get('cueCount'), 0, len(cues))):
        raise CoreError('音频检查点无效。', 'audio_incomplete')
    pending = cp.get('pendingTokens', [])
    if not isinstance(pending, list) or len(pending) > 4096:
        raise CoreError('检查点待分段token超限。', 'audio_incomplete')
    import math
    for token in pending:
        if (not isinstance(token, dict) or not isinstance(token.get('text'), str) or
                any(type(token.get(k)) not in (int, float) or not math.isfinite(token[k]) for k in ('start', 'end')) or
                not 0 <= token['start'] <= token['end'] <= cp['sample'] / RATE):
            raise CoreError('检查点token时间无效。', 'audio_time')
    if sum(len(d.get('text', '').encode()) for d in task['drafts'].values() if isinstance(d, dict) and isinstance(d.get('text'), str)) > MAX_INPUT_BYTES:
        raise CoreError('字幕草稿累计超过1MiB。', 'audio_limit')
    for key, draft in task['drafts'].items():
        cue = next((c for c in cues if c['id'] == key), None)
        if (not cue or not isinstance(draft, dict) or draft.get('baseRevision') != cue['revision'] or
                not isinstance(draft.get('text'), str) or len(draft['text'].encode()) > MAX_CUE_BYTES):
            raise CoreError('字幕草稿归属或容量无效。', 'draft_revision_mismatch')
        task_id(draft.get('id'))


def millis(sample):
    return (sample * 1000 + RATE // 2) // RATE


def timestamp(ms, separator='.'):
    return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02}{separator}{ms%1000:03}'


def subtitle_text(task, format='txt'):
    if format not in ('txt', 'srt', 'vtt'): raise CoreError('导出格式无效。')
    validate_audio_task(task); a = task['audio']
    labels = dict(pending='尚未翻译', processing='翻译处理中', failed='翻译失败', stopped='翻译中断')
    output = ['WEBVTT\n'] if format == 'vtt' else []
    if format == 'txt':
        output += [f"音频双语字幕 · {task['name']}\n任务 {task['id']} · 修订 {task['revision']}",
                   f"识别状态 {a['phase']} · 完成标记 {a['asrComplete']} · 待核对 {sum(not c['reviewed'] for c in a['cues'])}",
                   '父版本 ' + str(task.get('parent')),
                   f"空原文cue {sum(not trim(c['source']) for c in a['cues'])}，SRT/VTT略过空cue，TXT保留状态。",
                   '识别完成与准确完整分别判断，时间为模型估计，请回放核对。', a.get('error', '')]
    number = 0
    for c in a['cues']:
        target = c['result']['translation'] if c['state'] == 'completed' else '【' + labels[c['state']] + '】'
        start, end = millis(c['startSample']), millis(c['endSample'])
        media_end = millis(a['pcm']['samples'] if a['pcm'] else a.get('recording', {}).get('watermarks', {}).get('durable', 0))
        if format != 'txt':
            if media_end < 1: raise CoreError('音频短于1毫秒，无法生成合法字幕时间。', 'audio_time')
            start = min(start, media_end - 1)
            end = min(media_end, max(start + 1, end))
        if format == 'txt':
            output += [f"\n[{timestamp(start)} → {timestamp(end)}] cue {c['id']} · 修订 {c['revision']} · " +
                       ('已核对' if c['reviewed'] else '待核对'), '[原文]\n' + (c['source'] or '【人工清空】'),
                       '[译文]\n' + target, c.get('error', '')]
        elif trim(c['source']):
            number += 1
            def clean(value):
                value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
                value = '\n'.join(line.strip() for line in value.splitlines() if line.strip())
                return html.escape(value, quote=False).replace('--&gt;', '—&gt;')
            separator = ',' if format == 'srt' else '.'
            output += [f'{number}\n{timestamp(start, separator)} --> {timestamp(end, separator)}\n' +
                       clean(c['source']) + '\n' + clean(target) + '\n']
    return '\n'.join(output) + '\n'


def new_cue(identity, row, samples):
    import math
    start, end = row['start'], row['end']
    if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)) or end < start:
        raise CoreError('模型响应包含非有限或倒序时间。', 'audio_time')
    first = min(samples - 1, max(0, round(start * RATE)))
    last = min(samples, max(first + 1, round(end * RATE)))
    return dict(id=identity, revision=identity, source=row['english'], startSample=first, endSample=last,
                raw=deepcopy(row), state='pending', reviewed=False, reviewRevision=None, translationRevision=None)
