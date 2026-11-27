"""Bounded, opt-in media analysis using the board's existing offline models."""
import base64
import binascii
import queue
import secrets
import threading
import time
from pathlib import Path

from .audio.sherpa import SherpaStreamingClient

ROOT = Path(__file__).resolve().parent.parent.parent
BASE = str(ROOT / 'models' / 'vision') + '/'
ASR = dict(asr_worker_python=str(ROOT / 'dependencies' / 'asr-venv' / 'bin' / 'python'),
           asr_companion_src=str(ROOT / 'dependencies' / 'companion-src'),
           asr_model_dir=str(ROOT / 'models' / 'sherpa-onnx-streaming-paraformer-bilingual-zh-en'))
vision_lock = threading.Lock()
vision_models = None
sessions = {}
sessions_lock = threading.Lock()


def vision(encoded):
    global vision_models
    if not isinstance(encoded, str) or len(encoded) > 110000:
        raise ValueError('图像大小无效')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('图像格式无效')
    if not vision_lock.acquire(blocking=False):
        raise RuntimeError('视觉分析繁忙')
    try:
        import cv2
        import numpy as np
        from .vision.yolo_detector import YoloV8PersonDetector
        from .vision.hand_raise import PoseRKNNHandRaiseDetector
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[0] > 480 or image.shape[1] > 640:
            raise ValueError('图像尺寸无效')
        if vision_models is None:
            detector = YoloV8PersonDetector(BASE + 'yolov8n_rk3576_fp16.rknn')
            try:
                pose = PoseRKNNHandRaiseDetector(BASE + 'yolov8n-pose.rknn')
            except Exception:
                detector.release()
                raise
            vision_models = detector, pose
        start = time.monotonic()
        persons = vision_models[0].detect(image)
        raised = vision_models[1].detect(image, persons).raised
        return {'count': len(persons), 'raised': raised, 'milliseconds': round((time.monotonic()-start)*1000),
                'boxes': [item.to_dict() for item in persons[:50]]}
    finally:
        vision_lock.release()


class AudioSession:
    def __init__(self, emit):
        self.emit = emit
        self.identifier = secrets.token_hex(8)
        self.queue = queue.Queue(maxsize=8)
        self.closed = threading.Event()
        self.client = SherpaStreamingClient(ASR)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            self.client.start('meeting', lambda meeting, utterance, text, final, seq:
                              self.emit({'type':'transcript', 'text':text, 'final':final, 'utterance':self.identifier + ':' + utterance, 'sequence':seq}),
                              lambda state: self.emit({'type':'asr_status', 'state':state}))
            while not self.closed.is_set():
                try:
                    pcm = self.queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                self.client.feed(pcm)
        except Exception as error:
            self.emit({'type':'asr_status', 'state':'识别不可用：' + str(error)[:100]})
        finally:
            self.client.stop()
            self.closed.set()

    def feed(self, data):
        if self.closed.is_set():
            raise RuntimeError('语音识别已停止')
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            raise RuntimeError('语音识别繁忙，请重试')

    def stop(self):
        self.closed.set()


def start_audio(key, emit):
    with sessions_lock:
        old = sessions.pop(key, None)
        if old:
            old.stop()
        if len(sessions) >= 4:
            raise RuntimeError('语音识别已达并发上限')
        def emit_current(message):
            with sessions_lock:
                valid = sessions.get(key) in (None, current)
            if valid:
                emit(message)
        current = AudioSession(emit_current)
        sessions[key] = current
        current.start()


def audio(key, encoded):
    if not isinstance(encoded, str) or len(encoded) > 30000:
        raise ValueError('音频大小无效')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('音频格式无效')
    if not data or len(data) % 2 or len(data) > 20000:
        raise ValueError('音频格式无效')
    with sessions_lock:
        current = sessions.get(key)
    if current is None:
        raise RuntimeError('请先启动语音转写')
    current.feed(data)


def stop_audio(key):
    with sessions_lock:
        current = sessions.pop(key, None)
    if current:
        current.stop()
