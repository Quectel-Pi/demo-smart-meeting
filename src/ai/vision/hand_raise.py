"""YOLOv8n-Pose RKNN 姿态检测；告警去抖由网页负责。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .rknn_backend import RknnModel
from .yolo_detector import Detection, _nms_indices, _prediction_matrix, letterbox_rgb


@dataclass(frozen=True)
class HandRaiseObservation:
    raised: bool
    confidence: float = 0.0
    source: str = "none"


class HandRaiseDetectorInterface(ABC):
    """举手后端只输出观测，不直接操作 UI 或告警状态。"""

    @abstractmethod
    def detect(self, frame_bgr: np.ndarray, persons: list[Detection]) -> HandRaiseObservation:
        raise NotImplementedError

    def release(self) -> None:
        pass


class PoseRKNNHandRaiseDetector(HandRaiseDetectorInterface):
    """YOLOv8n-Pose RKNN 热插拔后端：腕部关键点高于同侧肩部即举手。

    支持常见单输出 [1,56,8400] / [8400,56]：4 坐标 + 1 置信度 + 17×3 关键点。
    """

    def __init__(self, model_path: str | Path, npu_core: str = "auto",
                 confidence: float = 0.45, keypoint_confidence: float = 0.45):
        if not Path(model_path).is_file():
            raise FileNotFoundError(f"姿态模型不存在: {model_path}")
        self.model = RknnModel(str(model_path), core_mask=npu_core)
        self.confidence = confidence
        self.keypoint_confidence = keypoint_confidence

    def detect(self, frame_bgr: np.ndarray, persons: list[Detection]) -> HandRaiseObservation:
        del persons  # 姿态模型独立检测 person + keypoints；保持统一检测接口。
        image, info = letterbox_rgb(frame_bgr, self.model.input_width, self.model.input_height)
        prediction = _prediction_matrix(self.model.run(image), 56)
        scores = prediction[:, 4].astype(np.float32)
        selected = np.flatnonzero(scores >= self.confidence)
        if selected.size == 0:
            return HandRaiseObservation(False, 0.0, "pose_rknn")
        boxes = prediction[selected, :4].astype(np.float32)
        scores = scores[selected]
        best = HandRaiseObservation(False, 0.0, "pose_rknn")
        for index in _nms_indices(boxes, scores, self.confidence):
            keypoints = prediction[selected[index], 5:56].reshape(17, 3).astype(np.float32)
            keypoints[:, 0] = (keypoints[:, 0] - info.pad_x) / info.scale
            keypoints[:, 1] = (keypoints[:, 1] - info.pad_y) / info.scale
            box_height = max(1.0, float(boxes[index, 3]) / info.scale)
            raised = any(
                self._wrist_above_shoulder(keypoints, wrist, shoulder, box_height)
                for wrist, shoulder in ((9, 5), (10, 6))
            )
            if raised and float(scores[index]) > best.confidence:
                best = HandRaiseObservation(True, float(scores[index]), "pose_rknn")
        return best

    def _wrist_above_shoulder(self, keypoints: np.ndarray, wrist: int, shoulder: int,
                              box_height: float) -> bool:
        wrist_point = keypoints[wrist]
        shoulder_point = keypoints[shoulder]
        if min(wrist_point[2], shoulder_point[2]) < self.keypoint_confidence:
            return False
        return bool(wrist_point[1] < shoulder_point[1] - max(12.0, box_height * 0.05))

    def release(self) -> None:
        self.model.release()
