"""Manual desktop run: no UI input automation, installer, or global service changes."""
import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.platform != 'win32': raise RuntimeError('Run on native Windows')
session = ctypes.c_ulong()
if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
    raise RuntimeError('Open this check yourself from the normal logged-in Windows desktop, not SSH')
parser = argparse.ArgumentParser(description='Native logged-in desktop check; own service tree only')
parser.add_argument('--candidate', type=Path, required=True)
parser.add_argument('--task-root', type=Path, required=True)
parser.add_argument('--evidence-root', type=Path, required=True)
parser.add_argument('--engine', type=Path, required=True)
parser.add_argument('--models', type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
candidate = args.candidate.resolve()
identity = json.loads((candidate / 'candidate.json').read_text(encoding='utf-8'))
package = candidate / 'B01'
sys.path[:0] = [str(package), str(package / 'shared')]
from translator_preview.runtime import owned_python, owned_environment
from translator_core.ollama import Ollama
from translator_core.contracts import default_recipe

from translator_preview.owned_process import OwnedWindowsProcess
evidence = args.evidence_root.resolve() / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
evidence.mkdir(parents=True)
tasks = args.task_root.resolve()
engine = args.engine.resolve()
models = args.models.resolve()
if not all(p.is_relative_to(root / 'artifacts') for p in (tasks, evidence, engine)):
    raise ValueError('Task/evidence/engine must remain in the project artifacts directory')
server = ui = None
report = dict(schema=1, candidate=identity, native_session_id=session.value,
              actual_desktop_actions='must_be_reported_by_the_user', ui_exit_code=None,
              task_root=str(tasks), original_service_stopped=False, remote_service_modified=False)

def snapshots():
    return {str(p.relative_to(tasks)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tasks.glob('*/current.json')}

try:
    # Reuse an existing compatible loopback service. Start only this official
    # fixed engine when the loopback port is genuinely unbound.
    import socket
    with socket.socket() as probe:
        occupied = probe.connect_ex(('127.0.0.1', 11434)) == 0
    if not occupied:
        if hashlib.sha256(engine.read_bytes()).hexdigest() != 'ecdb9fe1c52a08a0629d8ca658f9577b24f83a1600857fe31646f41b5dec52fc':
            raise RuntimeError('Fixed engine executable changed; preserve resources and recheck')
        environment = owned_environment()
        environment.update(OLLAMA_HOST='127.0.0.1:11434', OLLAMA_MODELS=str(models),
                           OLLAMA_NO_CLOUD='1', OLLAMA_MAX_LOADED_MODELS='1',
                           OLLAMA_NUM_PARALLEL='1', OLLAMA_NOPRUNE='1')
        with (evidence / 'ollama.stdout.log').open('wb') as out, (evidence / 'ollama.stderr.log').open('wb') as err:
            server = OwnedWindowsProcess([str(engine), 'serve'], env=environment, stdout=out, stderr=err)
        report['owned_server_pid'] = server.pid
        for attempt in range(60):
            if server.poll() is not None: raise RuntimeError('Owned engine exited before startup')
            try:
                if Ollama().json('/api/version')['version'] == '0.35.0': break
            except Exception: pass
            time.sleep(.5)
        else: raise RuntimeError('Fixed loopback engine did not become ready')
    report['reused_original_service'] = occupied
    report['verified_service'] = Ollama().verify(default_recipe('en-zh'))
    report['snapshots_before'] = snapshots()
    print('请在窗口中验证：无模型开始/取消/停止/重连；文字保存、主动翻译和TXT导出。')
    print('关闭后，再次双击本入口，打开已保存任务并确认没有自动翻译。')
    print('长文样本在本目录 GUI-长文样本.txt，可另建任务验证取消和主动继续。', flush=True)
    with (evidence / 'ui.stdout.log').open('wb') as out, (evidence / 'ui.stderr.log').open('wb') as err:
        ui = subprocess.Popen([owned_python(), '-I', '-B', '-X', 'utf8', str(package / 'launch.py'),
            '--evidence', str(evidence / 'ui-events.json'), '--screenshot', str(evidence / 'window.png'),
            '--task-root', str(tasks)], env=owned_environment(), cwd=package, stdout=out, stderr=err)
    report['actual_ui_pid'] = ui.pid
    report['ui_exit_code'] = ui.wait()
    report['snapshots_after'] = snapshots()
    print('实际窗口进程退出码：', report['ui_exit_code'], flush=True)
finally:
    if server and (ui is None or ui.poll() is not None):
        server.close()
        report['owned_job_closed_kills_descendants'] = True
    elif server:
        report['owned_server_kept_for_live_ui'] = True
    report['final_process_audit_pending'] = True
    report['ownership_policy'] = 'suspended CreateProcessW; private KILL_ON_JOB_CLOSE; assignment before resume; shared service untouched'
    (evidence / 'actual-process-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('本次详细证据：', evidence, flush=True)
