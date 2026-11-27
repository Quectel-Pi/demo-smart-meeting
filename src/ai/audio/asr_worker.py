#!/usr/bin/env python3
"""独立 sherpa-onnx 流式识别 worker，stdin/stdout 使用 NDJSON。"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time

import numpy as np

from robot_companion.audio.streaming_asr import CommandText, StreamingCommandRecognizer


class ASRWorker:
    def __init__(self, args):
        self.rate = args.rate
        self._lock = threading.Lock()
        self._final_seen = threading.Event()
        self._utterance = 1
        self._sequence = 0
        self._has_text = False
        config = {
            "rate": self.rate,
            "asr_streaming": {
                "enabled": True,
                "model_type": "paraformer",
                "model_dir": args.model_dir,
                "threads": args.threads,
                "rule2_min_trailing_silence": 0.8,
                "rule3_min_utterance_length": 15.0,
                "strip_wake_words": False,
                "strip_leading_fillers": "",
                "prefer_int8": True,
            },
        }
        self.recognizer = StreamingCommandRecognizer.from_config(
            config, on_text=self._on_text, threaded=True,
        )
        self.recognizer.open()

    def _emit(self, message):
        with self._lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _on_text(self, item: CommandText):
        if not item.text:
            return
        with self._lock:
            utterance_id = str(self._utterance)
            self._sequence += 1
            sequence = self._sequence
            if item.is_final:
                self._utterance += 1
                self._has_text = False
                self._final_seen.set()
            else:
                self._has_text = True
        self._emit({
            "type": "final" if item.is_final else "partial",
            "utterance_id": utterance_id,
            "sequence": sequence,
            "text": item.text,
        })

    def run(self):
        self._emit({"type": "ready", "sample_rate": self.rate})
        for raw in sys.stdin.buffer:
            try:
                message = json.loads(raw)
                command = message.get("command")
                if command == "audio":
                    pcm = base64.b64decode(message.get("pcm", ""))
                    self.recognizer.feed(np.frombuffer(pcm, dtype=np.int16))
                elif command == "stop":
                    self.flush()
                    return 0
            except Exception as error:
                self._emit({"type": "error", "message": str(error)[:240]})
                return 2
        self.flush()
        return 0

    def flush(self):
        self._final_seen.clear()
        before = self.recognizer.decode_seconds
        self.recognizer.feed(np.zeros(int(self.rate * 1.2), dtype=np.int16))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self._final_seen.is_set():
                break
            with self._lock:
                has_text = self._has_text
            if not has_text and self.recognizer.decode_seconds > before:
                break
            time.sleep(0.02)
        self.recognizer.close()
        self.recognizer.stop()
        self._emit({"type": "stopped"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args()
    try:
        worker = ASRWorker(args)
    except Exception as error:
        sys.stdout.write(json.dumps({"type": "error", "message": str(error)[:240]}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 1
    return worker.run()


if __name__ == "__main__":
    raise SystemExit(main())
