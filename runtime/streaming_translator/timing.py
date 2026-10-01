"""Media-time validation. Bounds repair is not acoustic forced alignment."""
import math


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def map_token(text, start, end, origin_sample, received_sample):
    """Apply the processor-local origin exactly once; preserve its raw estimate."""
    origin = origin_sample / 16000
    return validate_token({'text': text, 'start': None if finite(start) is None else origin + float(start),
        'end': None if finite(end) is None else origin + float(end),
        'raw_local': {'start': repr(start), 'end': repr(end), 'origin_sample': origin_sample}},
        duration=received_sample / 16000, floor=origin)


def validate_token(token, duration=None, floor=0):
    token = dict(token)
    start, end = finite(token.get('start')), finite(token.get('end'))
    raw = token.get('timing_raw', {'start': token.get('start'), 'end': token.get('end')})
    issues = list(token.get('timing_issues', []))
    if start is None or end is None:
        issues.append('nonfinite_or_missing')
        start = start if start is not None else end if end is not None else floor
        end = end if end is not None else start
    if end < start:
        issues.append('reversed')
        start, end = end, start
    upper = finite(duration)
    if start < floor or end < floor:
        issues.append('before_media_origin')
    start, end = max(floor, start), max(floor, end)
    if upper is not None:
        upper = max(floor, upper)
        if start > upper or end > upper:
            issues.append('after_received_media')
        start, end = min(start, upper), min(end, upper)
    if end - start < .001:
        issues.append('zero_duration')
        # A 1 ms representational span, inside the media, is explicitly marked.
        if upper is None or start + .001 <= upper:
            end = start + .001
        else:
            start = max(floor, end - .001)
    token.update(start=start, end=end, timing_raw=raw, timing_issues=sorted(set(issues)))
    return token


def segment_timing(tokens):
    spoken = [t for t in tokens if any(c.isalnum() for c in t['text'])]
    raw = [{'text': t['text'], 'estimate': t.get('timing_raw', {'start': t['start'], 'end': t['end']}),
            'local': t.get('raw_local')} for t in tokens]
    issues = {issue for t in tokens for issue in t.get('timing_issues', [])}
    if any(b['start'] < a['start'] - .75 for a, b in zip(spoken, spoken[1:])):
        issues.add('token_time_rewind')
    start, end = min(t['start'] for t in spoken), max(t['end'] for t in spoken)
    reasons = []
    if start != min(t['start'] for t in tokens) or end != max(t['end'] for t in tokens):
        reasons.append('punctuation_has_no_speech_extent')
    return start, end, {'raw_tokens': raw, 'issues': sorted(issues), 'reasons': reasons,
                        'status': 'needs_review' if issues else 'estimated'}


def effective_row(row, duration=None):
    """One definition for old/new snapshots, playback and all export formats."""
    row = dict(row)
    checked = validate_token(row, duration)
    row.update(start=checked['start'], end=checked['end'])
    issues = list(checked['timing_issues'])
    if issues:
        row['timing_status'] = 'needs_review'
        row['timing_note'] = '时间估计异常，已限制在录音范围内；请回放核对。'
    return row
