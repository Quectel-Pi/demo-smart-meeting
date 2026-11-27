#!/usr/bin/env python3
"""Single entry point for the meeting service and board window."""
import argparse
import fcntl
import json
import os
import signal
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / 'runtime'


def health(port):
    try:
        with urlopen(f'https://127.0.0.1:{port}/api/health', timeout=1,
                     context=ssl._create_unverified_context()) as response:
            result = json.load(response)
        return isinstance(result, dict) and result.get('app') == 'smart-local-meeting' and result.get('root') == str(ROOT)
    except (OSError, ValueError, URLError):
        return False


def port_in_use(port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def certificate():
    cert, key = RUNTIME / 'cert.pem', RUNTIME / 'key.pem'
    if cert.exists() and key.exists():
        return cert, key
    if cert.exists() or key.exists():
        raise RuntimeError('证书或私钥缺失；请检查 runtime 中的现有文件')
    ip = os.environ.get('MEETING_HOST_IP', '')
    if not ip:
        try:
            ip = subprocess.check_output(['hostname', '-I'], text=True).split()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            raise RuntimeError('请使用 MEETING_HOST_IP 指定开发板 IPv4 地址')
    import ipaddress
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        raise RuntimeError('MEETING_HOST_IP 必须是有效的 IPv4 地址')
    # Generate in temporary files so an interruption never leaves a partial pair.
    cert_tmp, key_tmp = RUNTIME / 'cert.pem.new', RUNTIME / 'key.pem.new'
    try:
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-sha256',
                        '-nodes', '-days', '365', '-keyout', str(key_tmp), '-out', str(cert_tmp),
                        '-subj', '/CN=smart-local-meeting', '-addext',
                        f'subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key_tmp.replace(key)
        cert_tmp.replace(cert)
    finally:
        cert_tmp.unlink(missing_ok=True)
        key_tmp.unlink(missing_ok=True)
    return cert, key


def process_start(pid):
    try:
        data = (Path('/proc') / str(pid) / 'stat').read_text()
        fields = data[data.rfind(')') + 2:].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def metadata_path(port):
    return RUNTIME / f'service-{port}.json'


def read_metadata(port):
    try:
        value = json.loads(metadata_path(port).read_text())
        if (isinstance(value, dict) and isinstance(value.get('pid'), int)
                and value.get('start') and process_start(value['pid']) == value['start']):
            return value
    except (OSError, ValueError):
        pass
    return None


def write_metadata(port, value):
    path = metadata_path(port)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value))
    temp.replace(path)


def set_service_mode(port, keep_alive):
    value = read_metadata(port)
    if value:
        value['keep_alive'] = keep_alive
        write_metadata(port, value)
    return value


def monitor(port, browser_pid, browser_start):
    profile = RUNTIME / 'browser-profile'
    url = f'https://127.0.0.1:{port}/'
    while True:
        while process_start(browser_pid) == browser_start:
            time.sleep(0.5)
        with (RUNTIME / 'launcher.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            replacement = window_running(profile, url)
            if replacement:
                browser_pid = replacement
                browser_start = process_start(replacement)
                if browser_start:
                    continue
            value = read_metadata(port)
            if value and not value.get('keep_alive'):
                # Only terminate a process started by this launcher and still matching its start time.
                try:
                    os.kill(value['pid'], signal.SIGTERM)
                except ProcessLookupError:
                    pass
                metadata_path(port).unlink(missing_ok=True)
            return


def start_monitor(port, browser_pid):
    value = read_metadata(port)
    if not value:
        print('现有服务不是由当前启动器管理；关闭窗口时不会自动停止该服务。', flush=True)
        return
    browser_start = process_start(browser_pid)
    if not browser_start:
        raise RuntimeError('会议窗口已退出，请重新启动。')
    watcher = value.get('watcher_pid')
    if (isinstance(watcher, int) and value.get('watcher_start') and
            process_start(watcher) == value['watcher_start']):
        return
    with (RUNTIME / 'launcher.log').open('ab') as log:
        watcher_process = subprocess.Popen([sys.executable, str(ROOT / 'src/launcher.py'),
                                            '--watch', str(port), str(browser_pid), browser_start],
                                           cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           start_new_session=True)
    value['watcher_pid'] = watcher_process.pid
    value['watcher_start'] = process_start(watcher_process.pid)
    write_metadata(port, value)


def ensure_server(port):
    if health(port):
        print(f'会议服务已运行，复用端口 {port}。', flush=True)
        return False
    if port_in_use(port):
        raise RuntimeError(f'端口 {port} 已被其他程序占用，未启动会议服务；请检查端口占用。')
    cert, key = certificate()
    with (RUNTIME / 'server.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, str(ROOT / 'src/server.py'), '--port', str(port),
                                    '--cert', str(cert), '--key', str(key)],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
    for _ in range(100):
        if health(port):
            start = process_start(process.pid)
            if not start:
                raise RuntimeError('会议服务意外退出。')
            write_metadata(port, {'pid':process.pid, 'start':start, 'keep_alive':False})
            print(f'会议服务已启动，端口 {port}。', flush=True)
            return True
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError('会议服务启动失败，请查看 runtime/server.log 并检查端口。')


def window_running(profile, url):
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            # Chromium may rewrite argv as a single NUL-terminated process title.
            command = b' ' + (proc / 'cmdline').read_bytes().replace(b'\0', b' ') + b' '
            if (f' --user-data-dir={profile} '.encode() in command and
                    f' --app={url} '.encode() in command):
                return int(proc.name)
        except (OSError, PermissionError):
            continue
    return None


def desktop_xauthority(display):
    # Use only an Xwayland server owned by this user for the exact local display.
    if not display or not display.startswith(':'):
        return None
    display = display.split('.', 1)[0]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            if (not args or Path(os.fsdecode(args[0])).name != 'Xwayland'
                    or display.encode() not in args or b'-auth' not in args):
                continue
            path = Path(os.fsdecode(args[args.index(b'-auth') + 1]))
            if path.is_absolute() and path.is_file() and path.stat().st_uid == os.getuid() and os.access(path, os.R_OK):
                return str(path)
        except (OSError, IndexError, ValueError):
            continue
    return None


def stop_started_server(port):
    value = read_metadata(port)
    if value and not value.get('keep_alive'):
        try:
            os.kill(value['pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass
        metadata_path(port).unlink(missing_ok=True)


def open_window(port):
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        print('当前终端没有桌面会话；请在开发板桌面终端再次运行 ./scripts/start.sh 打开窗口。', flush=True)
        return
    browser = Path(os.environ.get('MEETING_BROWSER', ROOT / 'dependencies/chromium/root/usr/lib/chromium/chromium'))
    if not browser.is_file() or not os.access(browser, os.X_OK):
        raise RuntimeError('未找到 Chromium；请检查 dependencies/chromium 或设置 MEETING_BROWSER。')
    profile = RUNTIME / 'browser-profile'
    profile.mkdir(exist_ok=True)
    url = f'https://127.0.0.1:{port}/'
    existing = window_running(profile, url)
    if existing:
        print('会议窗口已经打开。', flush=True)
        return existing
    env = os.environ.copy()
    for name in ('QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_FONTDIR', 'LD_LIBRARY_PATH', 'LD_PRELOAD'):
        env.pop(name, None)
    authority = desktop_xauthority(env.get('DISPLAY'))
    if authority and not env.get('XAUTHORITY'):
        env['XAUTHORITY'] = authority
    def launch():
        with (RUNTIME / 'browser.log').open('ab') as log:
            return subprocess.Popen([str(browser), f'--user-data-dir={profile}', '--allow-insecure-localhost',
                                     '--disable-background-mode', f'--app={url}'], cwd=ROOT, env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    process = launch()
    time.sleep(0.5)
    if process.poll() not in (None, 0) and authority and env.get('XAUTHORITY') != authority:
        env['XAUTHORITY'] = authority
        process = launch()
        time.sleep(0.5)
    if process.poll() not in (None, 0):
        raise RuntimeError('会议窗口启动失败，请检查桌面显示授权或查看 runtime/browser.log。')
    pid = window_running(profile, url)
    if not pid:
        raise RuntimeError('未找到会议窗口进程，请查看 runtime/browser.log。')
    print('会议窗口启动命令已执行。', flush=True)
    return pid


def main():
    if len(sys.argv) == 5 and sys.argv[1] == '--watch':
        monitor(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
        return 0
    parser = argparse.ArgumentParser(description='启动或复用会议服务并打开板端窗口')
    parser.add_argument('--server-only', action='store_true', help='仅启动或复用服务，不打开窗口')
    parser.add_argument('--port', type=int, default=8443, help='HTTPS 服务端口，默认 8443')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在 1 到 65535 之间')
    os.umask(0o077)
    RUNTIME.mkdir(exist_ok=True)
    try:
        with (RUNTIME / 'launcher.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = ensure_server(args.port)
            if args.server_only:
                set_service_mode(args.port, True)
            else:
                try:
                    browser_pid = open_window(args.port)
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    if started:
                        stop_started_server(args.port)
                    raise
                if browser_pid:
                    set_service_mode(args.port, False)
                    start_monitor(args.port, browser_pid)
        print(f'访问地址：https://<开发板IP>:{args.port}/', flush=True)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f'启动失败：{error}', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
