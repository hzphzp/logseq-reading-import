"""Foreground companion for the Logseq FileStorage API. Stop with Ctrl-C."""
import fcntl
import json
import os
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
STORE = Path.home() / '.logseq/storages/logseq-reading-import'
RUNS = Path.home() / '.local/share/logseq-reading-import/runs'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def graph_name(repo):
    import shutil
    cli = shutil.which('logseq') or str(Path.home() / '.local/bin/logseq')
    result = subprocess.run([cli, 'server', 'list', '-o', 'json'], capture_output=True, text=True, check=True)
    for server in json.loads(result.stdout)['data']['servers']:
        if server.get('repo') == repo and server.get('status') == 'ready':
            return server['graph']
    raise ValueError('请在 Logseq Desktop 打开所选数据库图谱。')


def run_job(job, job_id, previews):
    graph = graph_name(job['repo'])
    out = RUNS / job_id
    command = [sys.executable, '-I', str(BASE / 'weread_import.py'), '--graph', graph, '--output-dir', str(out)]
    if job['action'] == 'apply':
        preview_id = job.get('preview')
        if previews.pop(preview_id, None) != job['repo']:
            raise ValueError('此图谱需要重新预览。')
        command += ['--input', str(RUNS / preview_id / 'source.json'), '--apply']
    elif job['action'] != 'preview':
        raise ValueError('无法识别的操作。')
    out.mkdir(mode=0o700, parents=True)
    with (out / 'import.log').open('w') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        lines = (out / 'import.log').read_text().splitlines()
        raise ValueError((lines[-1][:260] if lines else '导入器运行失败') + '。日志：' + str(out / 'import.log'))
    counts = read(out / 'preview.json')
    if job['action'] == 'preview':
        previews[job_id] = job['repo']
    else:
        verified = read(out / 'result.json')['verified']
        if any(verified['new_' + key] for key in ('books', 'highlights', 'reviews')):
            raise ValueError('写入后核对未通过，请重新预览。')
    return counts


def serve():
    os.umask(0o077)
    STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STORE / 'helper.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session, previews = str(uuid.uuid4()), {}
        inbox, results = STORE / 'requests', STORE / 'results'
        for directory in (inbox, results):
            directory.mkdir(mode=0o700, exist_ok=True)
        print('助手已启动。在 Logseq 命令面板打开「微信读书」。按 Ctrl-C 退出。', flush=True)
        stopped = threading.Event()
        def heartbeat():
            while not stopped.is_set():
                write(STORE / 'helper.json', {'session': session, 'at': int(time.time() * 1000)})
                stopped.wait(2)
        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()
        try:
            while True:
                for message in inbox.glob('*.json'):
                    try:
                        job = read(message)
                    except (ValueError, FileNotFoundError):
                        continue  # The SDK may still be writing this file.
                    job_id = message.stem
                    message.unlink(missing_ok=True)  # A crashed write is never replayed.
                    try:
                        if str(uuid.UUID(job_id)) != job_id or not isinstance(job, dict):
                            continue
                        if job.get('session') != session or not 0 <= time.time() - job['at'] / 1000 <= 120:
                            raise ValueError('请求已过期，请重新预览。')
                        result = {'ok': True, 'counts': run_job(job, job_id, previews)}
                    except Exception as error:
                        result = {'ok': False, 'message': str(error)}
                    write(results / message.name, result)
                time.sleep(0.75)
        finally:
            stopped.set()
            pulse.join()
            (STORE / 'helper.json').unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        serve()
    except BlockingIOError:
        sys.exit('助手已在运行。')
    except KeyboardInterrupt:
        pass
