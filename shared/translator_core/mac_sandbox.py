"""Enter a Mac policy with one exclusively owned OpenMP registration file."""
import json
import os
from pathlib import Path
import stat
import sys
import time


def sandbox_command(profile, command, folder):
    return [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
            profile, str(folder), *map(str, command)]


def release_temporary(folder, pid):
    marker = Path(folder) / 'sandbox-temporary.json'
    try:
        if not marker.is_file():
            return True
        owner = json.loads(marker.read_bytes())
        if owner['pid'] != pid or owner['parent_pid'] != os.getpid():
            return False
        path = Path('/private/tmp') / ('__KMP_REGISTERED_LIB_' + str(pid))
        current = path.lstat()
        if (stat.S_ISREG(current.st_mode) and current.st_uid == os.getuid() and
                current.st_ctime_ns >= owner['started_ns']):
            path.unlink()
            return True
    except FileNotFoundError:
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return False


def main():
    profile, folder, *command = sys.argv[1:]
    path = Path('/private/tmp') / ('__KMP_REGISTERED_LIB_' + str(os.getpid()))
    started_ns = time.time_ns()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    try:
        marker = dict(pid=os.getpid(), parent_pid=os.getppid(), path=str(path),
                      started_ns=started_ns, initially_absent=True,
                      purpose='OpenMP process registration', initialization='owned by OpenMP')
        (Path(folder) / 'sandbox-temporary.json').write_text(json.dumps(marker) + '\n')
        # OpenMP must initialize the registration itself. An existing empty or
        # zero-filled file can produce SIGBUS or a duplicate-runtime rejection.
        path.unlink()
    except BaseException:
        if path.exists():
            path.unlink()
        raise
    finally:
        os.close(descriptor)
    profile += '(allow file-write* (literal ' + json.dumps(str(path)) + '))'
    os.execv('/usr/bin/sandbox-exec', ['sandbox-exec', '-p', profile, *command])


if __name__ == '__main__':
    main()
