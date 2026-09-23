"""Conservative sentence boundaries over confirmed ASR tokens, never drafts."""
import re


ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "no", "fig", "approx", "inc"}


def sentence_cuts(text, final=False):
    """Character offsets; require lookahead so packet boundaries cannot split 3.14."""
    cuts = []
    for match in re.finditer(r'[.!?。！？]+[\"\u201d\u2019\)\]]*', text):
        end = match.end()
        if end == len(text) and not final:
            continue
        if end < len(text) and not text[end].isspace():
            continue
        if match.group().startswith('.'):
            before = text[:match.start()]
            word = re.search(r'([\w.]+)$', before)
            word = word.group(1) if word else ''
            if word.lower() in ABBREVIATIONS or re.fullmatch(r'[A-Za-z]', word):
                continue
            if re.fullmatch(r'(?:[A-Za-z]\.)+[A-Za-z]', word):
                continue
        cuts.append(end)
    return cuts


def take_segments(tokens, final=False, max_seconds=30, max_chars=1200):
    """Consume whole tokens; preserve spoken text, order, and genuine repetitions.

    Tokens carry original text and estimated times. No text-based deduplication.
    Long fragments get an explicit boundary reason, not an assertion of completeness.
    """
    tokens = list(tokens)
    segments = []
    while tokens:
        text = ''.join(t['text'] for t in tokens)
        cuts = sentence_cuts(text, final=final)
        boundary = cuts[0] if cuts else None
        reason = 'sentence'
        if boundary is None:
            pause_index = next((i for i in range(1, len(tokens)) if tokens[i]['start']-tokens[i-1]['end'] >= 2.0), None)
            if pause_index is not None:
                count, reason = pause_index, 'pause'
            elif len(text) >= max_chars or tokens[-1]['end']-tokens[0]['start'] >= max_seconds:
                count, reason = len(tokens), 'length_limit'
            elif final:
                count, reason = len(tokens), 'end_of_input'
            else:
                break
        else:
            length, count = 0, 0
            for token in tokens:
                length += len(token['text'])
                count += 1
                if length >= boundary:
                    break
            # Never split an ASR token; an unusual multi-sentence token stays together.
        selected, tokens = tokens[:count], tokens[count:]
        source = ''.join(t['text'] for t in selected).strip()
        # A decoder may confirm standalone periods/ellipses around pauses. They
        # are not speech and must not create subtitle rows or translation jobs.
        # The original event payload remains in SessionStore for inspection.
        if any(char.isalnum() for char in source):
            segments.append({'english': source, 'start': min(t['start'] for t in selected),
                             'end': max(t['end'] for t in selected), 'boundary': reason})
    return segments, tokens
