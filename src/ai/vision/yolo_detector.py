"""普通 YOLOv8n RKNN 的 letterbox 和 person 检测后处理。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .rknn_backend import RknnModel


@dataclass(frozen=True)
class LetterboxInfo:
    scale: float
    pad_x: float
    pad_y: float
    original_width: int
    original_height: int


@dataclass(frozen=True)
class Detection:
    x: float
    y: float
    width: float
    height: float
    confidence: float

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 5),
            "y": round(self.y, 5),
            "width": round(self.width, 5),
            "height": round(self.height, 5),
            "confidence": round(self.confidence, 4),
        }


def letterbox_rgb(image_bgr: np.ndarray, width: int, height: int) -> tuple[np.ndarray, LetterboxInfo]:
    original_height, original_width = image_bgr.shape[:2]
    scale = min(width / original_width, height / original_height)
    resized_width = max(1, round(original_width * scale))
    resized_height = max(1, round(original_height * scale))
    resized = cv2.resize(image_bgr, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((height, width, 3), 114, dtype=np.uint8)
    pad_x = (width - resized_width) // 2
    pad_y = (height - resized_height) // 2
    canvas[pad_y:pad_y + resized_height, pad_x:pad_x + resized_width] = resized
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
    info = LetterboxInfo(scale, float(pad_x), float(pad_y), original_width, original_height)
    return np.ascontiguousarray(rgb), info


def _prediction_matrix(outputs: list[np.ndarray], channels: int) -> np.ndarray:
    if not outputs:
        raise RuntimeError("RKNN 模型没有输出张量")
    prediction = np.asarray(outputs[0]).squeeze()
    if prediction.ndim != 2:
        raise RuntimeError(f"不支持的 YOLO 输出形状: {prediction.shape}")
    if prediction.shape[0] == channels and prediction.shape[1] > prediction.shape[0]:
        prediction = prediction.T
    if prediction.shape[1] < channels:
        raise RuntimeError(f"YOLO 输出通道不足: {prediction.shape}，期望至少 {channels}")
    return prediction


def _nms_indices(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    top_left = boxes.copy()
    top_left[:, 0] -= boxes[:, 2] / 2
    top_left[:, 1] -= boxes[:, 3] / 2
    indices = cv2.dnn.NMSBoxes(top_left.tolist(), scores.tolist(), 0.0, threshold)
    if len(indices) == 0:
        return []
    return np.asarray(indices).reshape(-1).tolist()


def _restore_box(box: np.ndarray, info: LetterboxInfo) -> tuple[float, float, float, float]:
    center_x, center_y, width, height = [float(value) for value in box]
    x1 = (center_x - width / 2 - info.pad_x) / info.scale
    y1 = (center_y - height / 2 - info.pad_y) / info.scale
    x2 = (center_x + width / 2 - info.pad_x) / info.scale
    y2 = (center_y + height / 2 - info.pad_y) / info.scale
    x1 = float(np.clip(x1, 0, info.original_width))
    y1 = float(np.clip(y1, 0, info.original_height))
    x2 = float(np.clip(x2, 0, info.original_width))
    y2 = float(np.clip(y2, 0, info.original_height))
    return x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1)


class YoloV8PersonDetector:
    """仅使用普通 YOLOv8n 的 person 类别（COCO class 0）。"""

    def __init__(self, model_path: str | Path, npu_core: str = "auto",
                 confidence: float = 0.45, nms: float = 0.45):
        self.model = RknnModel(str(model_path), core_mask=npu_core)
        self.confidence = confidence
        self.nms = nms

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        image, info = letterbox_rgb(frame_bgr, self.model.input_width, self.model.input_height)
        prediction = _prediction_matrix(self.model.run(image), 84)
        scores = prediction[:, 4].astype(np.float32)
        selected = np.flatnonzero(scores >= self.confidence)
        if selected.size == 0:
            return []
        boxes = prediction[selected, :4].astype(np.float32)
        scores = scores[selected]
        detections: list[Detection] = []
        for index in _nms_indices(boxes, scores, self.nms):
            x, y, width, height = _restore_box(boxes[index], info)
            if width <= 1 or height <= 1:
                continue
            detections.append(Detection(
                x=x / info.original_width,
                y=y / info.original_height,
                width=width / info.original_width,
                height=height / info.original_height,
                confidence=float(scores[index]),
            ))
        return sorted(detections, key=lambda item: item.confidence, reverse=True)

    def release(self) -> None:
        self.model.release()
