"""Low-frequency OS measurements; peak RSS is never presented as current RSS."""
import os
import resource
import subprocess
import time


def process_metrics():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    current = None
    try:
        result = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(os.getpid())],
                                capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            current = int(result.stdout.strip()) * 1024
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {'worker_pid': os.getpid(), 'worker_rss_bytes': current,
            'worker_peak_rss_bytes': usage.ru_maxrss,
            'worker_cpu_seconds': usage.ru_utime + usage.ru_stime,
            'monotonic_seconds': time.monotonic()}
