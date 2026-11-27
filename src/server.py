#!/usr/bin/env python3
"""LAN WebRTC signalling and opt-in local AI analysis server."""
import argparse
import json
import mimetypes
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from ai import service as ai

ROOT = Path(__file__).resolve().parent / 'static'
LOCK = threading.RLock()
ROOMS = {}
MAX_PEERS = 4


def cleanup():
    now = time.monotonic()
    for number, room in list(ROOMS.items()):
        for pid, peer in list(room.items()):
            if now - peer['seen'] > 45:
                del room[pid]
                ai.stop_audio((number, pid))
                for other in room.values():
                    other['queue'].append({'type': 'left', 'peer': pid})
        if not room:
            del ROOMS[number]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, fmt, *args):
        pass

    def reply(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/health':
            return self.reply(200, {'app': 'smart-local-meeting', 'root': str(ROOT.parent.parent)})
        name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}.get(path)
        if not name:
            return self.reply(404, {'error': '不存在'})
        data = (ROOT / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', (mimetypes.guess_type(name)[0] or 'text/plain') + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; media-src 'self' blob:; img-src 'self' data:")
        self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        origin = self.headers.get('Origin')
        if origin and origin != 'https://' + self.headers.get('Host', ''):
            return self.reply(403, {'error': '来源不受允许'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length <= 0 or length > 131072:
                return self.reply(400, {'error': '请求大小错误'})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, json.JSONDecodeError):
            return self.reply(400, {'error': '请求格式错误'})
        path = urlsplit(self.path).path
        if path.startswith('/api/ai/'):
            return self.handle_ai(path, body)
        with LOCK:
            cleanup()
            if path == '/api/create':
                for _ in range(100):
                    number = f'{secrets.randbelow(900000000) + 100000000}'
                    if number not in ROOMS:
                        ROOMS[number] = {}
                        break
                else:
                    return self.reply(503, {'error': '创建失败'})
                return self.join(number)
            if path == '/api/join':
                number = str(body.get('room', '')).replace(' ', '')
                if number not in ROOMS:
                    return self.reply(404, {'error': '会议不存在或已结束'})
                return self.join(number)
            number, pid, token = body.get('room'), body.get('peer'), body.get('token')
            room = ROOMS.get(number, {})
            peer = room.get(pid)
            if not peer or not isinstance(token, str) or not secrets.compare_digest(peer['token'], token):
                return self.reply(403, {'error': '会议已断开，请重新加入'})
            peer['seen'] = time.monotonic()
            if path == '/api/poll':
                messages = peer['queue'][:]
                peer['queue'].clear()
                return self.reply(200, {'messages': messages})
            if path == '/api/signal':
                target, message = room.get(body.get('target')), body.get('message')
                if not target or not isinstance(message, dict) or message.get('type') not in ('offer', 'answer', 'candidate', 'screen'):
                    return self.reply(400, {'error': '信令无效'})
                if len(target['queue']) >= 200:
                    return self.reply(429, {'error': '消息过多'})
                target['queue'].append({'type': 'signal', 'peer': pid, 'message': message})
                return self.reply(200, {'ok': True})
            if path == '/api/leave':
                ai.stop_audio((number, pid))
                del room[pid]
                for other in room.values():
                    other['queue'].append({'type': 'left', 'peer': pid})
                if not room:
                    del ROOMS[number]
                return self.reply(200, {'ok': True})
            return self.reply(404, {'error': '不存在'})

    def handle_ai(self, path, body):
        number, pid, token = body.get('room'), body.get('peer'), body.get('token')
        with LOCK:
            cleanup()
            peer = ROOMS.get(number, {}).get(pid)
            if not peer or not isinstance(token, str) or not secrets.compare_digest(peer['token'], token):
                return self.reply(403, {'error': '会议已断开，请重新加入'})
            peer['seen'] = time.monotonic()
        key = (number, pid)
        def emit(message):
            with LOCK:
                room = ROOMS.get(number, {})
                if room.get(pid) is not peer:
                    return
                for recipient in room.values():
                    if len(recipient['queue']) < 200:
                        recipient['queue'].append(dict(message, peer=pid))
        try:
            if path == '/api/ai/vision':
                result = ai.vision(body.get('image'))
                emit({'type':'vision', 'count':result['count'], 'raised':result['raised'],
                      'milliseconds':result['milliseconds']})
                return self.reply(200, result)
            if path == '/api/ai/audio-start':
                with LOCK:
                    if ROOMS.get(number, {}).get(pid) is not peer:
                        raise ValueError('会议已结束')
                    ai.start_audio(key, emit)
            elif path == '/api/ai/audio':
                ai.audio(key, body.get('pcm'))
            elif path == '/api/ai/audio-stop':
                ai.stop_audio(key)
            else:
                return self.reply(404, {'error':'不存在'})
            return self.reply(200, {'ok':True})
        except ValueError as error:
            return self.reply(400, {'error':str(error)})
        except Exception as error:
            return self.reply(503, {'error':str(error)[:160]})

    def join(self, number):
        room = ROOMS[number]
        if len(room) >= MAX_PEERS:
            return self.reply(409, {'error': '会议已满（最多 4 个设备）'})
        pid, token = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
        existing = list(room)
        room[pid] = {'token': token, 'queue': [], 'seen': time.monotonic()}
        return self.reply(200, {'room': number, 'peer': pid, 'token': token, 'existing': existing})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f'Local meeting service listening on {args.host}:{args.port}', flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
