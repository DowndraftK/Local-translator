"""Small per-processor diagnostics; never guess words or reset unconfirmed audio."""
from collections import deque


class OutputWatch:
    def __init__(self, threshold=10):
        self.threshold = threshold
        self.spans = deque()
        self.last_output = 0.
        self.reported = False

    def span(self, start, end, active):
        if self.spans and self.spans[-1][1] == start and self.spans[-1][2] == active:
            a, _, on = self.spans.pop()
            self.spans.append((a, end, on))
        else:
            self.spans.append((start, end, active))

    def observe(self, end, has_words):
        if has_words:
            self.last_output = end
            self.reported = False
        active = sum(max(0, min(end, b)-max(self.last_output, a)) for a,b,on in self.spans if on)
        issue = None
        if active >= self.threshold and not self.reported:
            first_active = min(max(self.last_output, a) for a,b,on in self.spans if on and b > self.last_output and a < end)
            issue = {'kind': 'no_committed_words', 'start': first_active, 'end': end,
                     'active_seconds': active,
                     'note': '此处有送入识别器的音频但长时间没有确认文字；请回放核对。'}
            self.reported = True
        # Diagnostic history is bounded even during hours of empty model output.
        while self.spans and self.spans[0][1] < max(self.last_output, end-60):
            self.spans.popleft()
        return issue
