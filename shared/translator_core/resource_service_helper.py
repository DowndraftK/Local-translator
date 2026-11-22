"""Disposable parent-death supervisor for the pinned local translation engine."""
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def main():
    request = json.loads(Path(sys.argv[1]).read_bytes())
    folder = Path(request['folder'])
    parent = request['parent_pid']
    if parent != os.getppid():
        raise RuntimeError('service_parent_mismatch')
    binary = Path(request['binary'])
    with binary.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != 'add45eb02df0252fb888549d277a66d6b551801869140d1c280bf11689e81b77':
            raise RuntimeError('engine_untrusted')
    def write(name, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        temporary = folder/('.'+name)
        with temporary.open('wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder/name)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    endpoint = 'http://127.0.0.1:'+str(port)
    env = {k:v for k,v in os.environ.items() if k in ('LANG','LC_ALL','LC_CTYPE')}
    env.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', HOME=str(folder), TMPDIR=str(folder),
               OLLAMA_HOST=endpoint, OLLAMA_MODELS=request['models_root'], OLLAMA_NO_CLOUD='true',
               OLLAMA_NOPRUNE='true', OLLAMA_NUM_PARALLEL='1', XDG_CACHE_HOME=str(folder/'cache'))
    escaped = str(folder).replace('\\','\\\\').replace('"','\\"')
    profile = ('(version 1)(allow default)(deny network-outbound)'
               '(allow network-outbound (remote ip "localhost:*"))(deny file-write*)'
               '(allow file-write* (subpath "'+escaped+'") (literal "/dev/null"))')
    child = subprocess.Popen(['/usr/bin/sandbox-exec','-p',profile,str(binary),'serve'],
                             cwd=folder, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    write('owner.json',dict(pid=child.pid,pgid=child.pid,parent_pid=os.getpid(),endpoint=endpoint,
                           binary=str(binary),models_root=request['models_root']))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_:stop.set())
    signal.signal(signal.SIGINT, lambda *_:stop.set())
    count = [0]
    def logs():
        with child.stdout, (folder/'server.log').open('wb') as output:
            while data := child.stdout.read1(4096):
                count[0] += len(data)
                if count[0] <= 8*1024*1024:
                    output.write(data)
                    output.flush()
                else:
                    stop.set()
    reader = threading.Thread(target=logs)
    reader.start()
    began = time.monotonic()
    failure = None
    readiness_requests = 0
    try:
        ready = False
        while child.poll() is None and not stop.wait(.5) and os.getppid() == parent:
            if not ready:
                if time.monotonic()-began > 45 or readiness_requests >= 64:
                    raise TimeoutError('service_startup')
                connection = http.client.HTTPConnection('127.0.0.1',port,timeout=.25)
                try:
                    readiness_requests += 1
                    connection.request('GET','/api/version')
                    response = connection.getresponse()
                    body = response.read(4097)
                    if response.status != 200 or len(body)>4096 or json.loads(body).get('version')!='0.35.0':
                        continue
                    listener = subprocess.run(['/usr/sbin/lsof','-nP','-a','-p',str(child.pid),
                                               '-iTCP:'+str(port),'-sTCP:LISTEN'],capture_output=True,text=True,timeout=2)
                    if listener.returncode or '127.0.0.1:'+str(port) not in listener.stdout:
                        continue
                    ready=True
                    write('ready.json',dict(version='owned-ollama-ready-v1',endpoint=endpoint,
                                          supervisor_pid=os.getpid(),engine_pid=child.pid,version_response=json.loads(body),
                                          readiness_requests=readiness_requests))
                except (OSError,ValueError,http.client.HTTPException):
                    pass
                finally:
                    connection.close()
    except BaseException as exc:
        failure = repr(exc)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGTERM)
                child.wait(timeout=5)
        # Scope is the dedicated process group created above.
        try:
            os.killpg(child.pid,signal.SIGTERM)
        except ProcessLookupError:
            pass
        reader.join(timeout=2)
        write('terminal.json',dict(OS_exit=child.returncode,pid=child.pid,supervisor_pid=os.getpid(),
                                   readers_drained=not reader.is_alive(),log_bytes=count[0],
                                   seconds=time.monotonic()-began,failure=failure,readiness_requests=readiness_requests))


if __name__=='__main__':
    main()
