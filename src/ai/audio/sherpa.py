"""独立 sherpa-onnx 流式 ASR 子进程客户端。"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from collections.abc import Callable


class SherpaStreamingClient:
    def __init__(self, config: dict):
        self.config = config
        self.python = str(config.get("asr_worker_python", ""))
        self.companion_src = str(config.get("asr_companion_src", ""))
        self.model_dir = str(config.get("asr_model_dir", ""))
        self.sample_rate = int(config.get("sample_rate", 16000))
        self.threads = int(config.get("asr_threads", 1))
        self._process: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._accepting = False
        self._stopping = False
        self._error = ""
        self._meeting_id = ""
        self._on_text: Callable | None = None
        self._on_status: Callable[[str], None] | None = None
        self._readers: list[threading.Thread] = []

    @property
    def active(self) -> bool:
        return self._accepting and self._process is not None and self._process.poll() is None

    def start(self, meeting_id: str, on_text: Callable, on_status: Callable[[str], None]) -> None:
        if not self.python or not self.companion_src or not self.model_dir:
            raise RuntimeError("sherpa worker、companion 源码或模型目录未配置")
        if not Path(self.python).is_file():
            raise RuntimeError(f"sherpa Python 不存在: {self.python}")
        if not Path(self.model_dir).is_dir():
            raise RuntimeError(f"流式模型目录不存在: {self.model_dir}")
        self.stop()
        self._meeting_id = meeting_id
        self._on_text = on_text
        self._on_status = on_status
        self._error = ""
        self._ready.clear()
        self._stopped.clear()
        self._stopping = False
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = self.companion_src + (os.pathsep + existing if existing else "")
        worker = Path(__file__).with_name("asr_worker.py")
        self._process = subprocess.Popen([
            self.python, str(worker), "--model-dir", self.model_dir,
            "--rate", str(self.sample_rate), "--threads", str(self.threads),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
        self._readers = [
            threading.Thread(target=self._read_stdout, args=(self._process,), name="sherpa-events", daemon=True),
            threading.Thread(target=self._read_stderr, args=(self._process,), name="sherpa-log", daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        on_status("ASR_LOADING")
        if not self._ready.wait(25):
            self.stop()
            raise RuntimeError("sherpa worker 初始化超时")
        if self._error:
            error = self._error
            self.stop()
            raise RuntimeError(error)
        self._accepting = True
        on_status("ASR_STREAMING")

    def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("sherpa worker 未运行")
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._write_lock:
            process.stdin.write(payload)
            process.stdin.flush()

    def feed(self, pcm: bytes) -> None:
        if self._accepting:
            self._send({"command": "audio", "pcm": base64.b64encode(pcm).decode("ascii")})

    def _read_stdout(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            try:
                message = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            kind = message.get("type")
            if kind == "ready":
                self._ready.set()
            elif kind == "stopped":
                self._stopped.set()
            elif kind == "error":
                self._error = str(message.get("message", "unknown sherpa error"))
                self._ready.set()
                self._stopped.set()
                if self._on_status and not self._stopping:
                    self._on_status(f"ASR ERROR // {self._error}"[:120])
            elif kind in ("partial", "final") and self._on_text:
                utterance = f"{self._meeting_id}:sherpa:{message.get('utterance_id', '')}"
                self._on_text(
                    self._meeting_id, utterance, str(message.get("text", "")),
                    kind == "final", int(message.get("sequence", 0)),
                )
        if not self._stopping and not self._error:
            self._error = "sherpa worker 意外退出"
            self._ready.set()
            if self._on_status:
                self._on_status("ASR_UNAVAILABLE")

    def _read_stderr(self, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            sys.stderr.buffer.write(b"SHERPA: " + raw)
            sys.stderr.buffer.flush()

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._stopping = True
        self._accepting = False
        if process.poll() is None:
            try:
                self._send({"command": "stop"})
                self._stopped.wait(5)
            except (OSError, RuntimeError):
                pass
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if process.stdin:
            process.stdin.close()
        for reader in self._readers:
            reader.join(timeout=1)
        self._readers = []
        self._process = None
        self._on_text = None
        self._on_status = None
