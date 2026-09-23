"""Sample-exact delayed VAD gate. Keep onset audio until delayed decisions arrive."""
import numpy as np


class LookbackVAD:
    def __init__(self, preroll=16000, postroll=4000, decision_delay=8000):
        self.preroll, self.postroll = preroll, postroll
        self.hold = preroll + decision_delay
        self.received = self.emitted = 0
        self.buffer = np.empty(0, dtype=np.float32)
        self.intervals = []
        self.open_start = None
        self.late_events = 0

    def feed(self, pcm, events=(), final=False):
        self.buffer = np.concatenate((self.buffer, pcm))
        self.received += len(pcm)
        for event in events:
            if 'start' in event:
                start = max(0, int(event['start']) - self.preroll)
                self.late_events += start < self.emitted
                self.open_start = max(self.emitted, start)
            elif 'end' in event and self.open_start is not None:
                self.intervals.append((self.open_start, int(event['end']) + self.postroll))
                self.open_start = None
        limit = self.received if final else max(self.emitted, self.received - self.hold)
        intervals = self.intervals + ([(self.open_start, self.received)] if self.open_start is not None else [])
        # Overlapping padded utterances form one span; repeated words stay untouched.
        merged = []
        for start, end in sorted(intervals):
            start, end = max(self.emitted, start), min(limit, end)
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        spans, cursor = [], self.emitted
        for start, end in merged:
            if cursor < start:
                spans.append((cursor, start, False, self.buffer[cursor-self.emitted:start-self.emitted].copy()))
            spans.append((start, end, True, self.buffer[start-self.emitted:end-self.emitted].copy()))
            cursor = end
        if cursor < limit:
            spans.append((cursor, limit, False, self.buffer[cursor-self.emitted:limit-self.emitted].copy()))
        self.buffer = self.buffer[limit-self.emitted:].copy()
        self.emitted = limit
        self.intervals = [(a, b) for a, b in self.intervals if b > limit]
        return spans
